"""
app/domains/workflow/task_service.py
Act 2 Autonomous Coding Build + Live HTML Preview & Act 4 Independent Read-Only Review Service
powered by `langchain-ai/deepagents` (`DeepAgentRuntime`).

Key guarantees:
  - Zero hardcoded code templates or mock fallbacks: missing LLM credentials fail fast with
    `LLMConfigurationError` (HTTP 503 `⛔ LLM CONFIG REQUIRED`).
  - Antigravity 3-phase workflow (`PLANNING -> EXECUTION -> VERIFICATION/WALKTHROUGH`):
    stores `implementation_plan.md`, `task.md`, and stamped `walkthrough.md` outside the
    worktree; supports `AGENT_PLAN_REVIEW=request_review` (`interrupt()` + `/plan/approve`)
    and `always_proceed`; derives commit message & MR description from `walkthrough.md`.
  - Gate-protected Git hygiene: commits and pushes ONLY when `DeepAgentRuntime` gates pass;
    never mutates the user's original local folder or `main`.
  - Act 4 Read-Only Review: detached worktree + `ReadOnlyGuardedBackend` + structured
    `ReviewFindings` merged via `EvaluatorRubric.merge_llm_findings`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.core.config import AppSettings, load_settings
from app.core.git_source import InternalGitSourceResolver, sanitize_git_text
from app.core.llm.provider_rotator import LLMProviderRotator
from app.domains.execution.artifact_workflow import PlanReviewPolicy, default_plan_review_policy
from app.domains.execution.code_mode import CODE_MODE_TOOL_DESCRIPTION, CodeModeExecutor
from app.domains.execution.deepagent_backend import (
    GuardedWorktreeBackend,
    ReadOnlyGuardedBackend,
    _safe_env,
    collect_env_secrets,
)
from app.domains.execution.runtime import DeepAgentResult, DeepAgentRuntime
from app.domains.guardrails.isolation_policy import WorktreeIsolationPolicy
from app.domains.guardrails.permissions import PermissionRules
from app.domains.workflow.evaluator import EvaluatorRubric
from app.domains.workflow.schemas import ReviewFindings
from app.domains.workflow.worktree_adapter import GitWorktreeManager, WorktreeInfo

_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,32}$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9_./@^~:{}-]+$")
_COMMIT_RANGE_RE = re.compile(r"^[A-Za-z0-9_./@^~:{}-]+(?:\.\.\.?[A-Za-z0-9_./@^~:{}-]+)?$")
_SNAPSHOT_MAX_FILES = 2000
_SNAPSHOT_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB per file
_SNAPSHOT_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".abyss",
        ".agents",
        "demo_workspace",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
    }
)


class _CodeModeToolInput(BaseModel):
    code: str = Field(
        description="Async Python snippet using await sh/read/write/edit/ls/gather. Last expression is returned."
    )


def _validate_git_ref(ref: str, label: str = "git ref") -> str:
    if any(ch in ref for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"Invalid {label}: control characters are not allowed.")
    cleaned = ref.strip()
    if (
        not cleaned
        or cleaned.startswith("-")
        or ".." in cleaned
        or not _GIT_REF_RE.fullmatch(cleaned)
    ):
        raise ValueError(f"Invalid {label}: {cleaned!r}")
    return cleaned


def _validate_commit_range(commit_range: str) -> str:
    if any(ch in commit_range for ch in ("\r", "\n", "\x00")):
        raise ValueError("Invalid commit_range: control characters are not allowed.")
    cleaned = commit_range.strip()
    if not cleaned or cleaned.startswith("-") or not _COMMIT_RANGE_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid commit_range: {cleaned!r}")
    if "..." in cleaned:
        parts = cleaned.split("...", 1)
    elif ".." in cleaned:
        parts = cleaned.split("..", 1)
    else:
        parts = [cleaned]
    for part in parts:
        _validate_git_ref(part, label="commit_range endpoint")
    return cleaned


# =============================================================================
# Act 4 Preset 4-A: Realistic Defective Dashboard MR Diff (REQUEST_CHANGES)
# =============================================================================
DEFECTIVE_MR_SAMPLE_DIFF = """diff --git a/inspection_dashboard/api_handler.py b/inspection_dashboard/api_handler.py
new file mode 100644
--- /dev/null
+++ b/inspection_dashboard/api_handler.py
@@ -0,0 +1,22 @@
+\"\"\"Quick API handler for pushing live inspection metrics to internal monitor.\"\"\"
+
+import json
+
+API_SECRET = "sk-prod-live-9876543210-internal"
+
+
+def fetch_service_payload(raw_body: str) -> dict:
+    try:
+        parsed = json.loads(raw_body)
+    except:
+        pass
+    return {"status": "ok", "token_loaded": bool(API_SECRET)}
+
+
+def evaluate_filter_expression(expr: str) -> bool:
+    try:
+        return bool(expr)
+    except Exception:
+        pass
+    return False
"""


# =============================================================================
# Declarative Build Preset Specifications (Zero Hardcoded Code Templates)
# =============================================================================
@dataclass(frozen=True)
class BuildPresetSpec:
    id: Literal["project_map_app", "inspection_dashboard", "custom"]
    label: str
    badge: str
    instruction: str
    preview_rel_path: str
    completion_cmd: str
    rubric: str


BUILD_PRESET_SPECS: dict[str, BuildPresetSpec] = {
    "project_map_app": BuildPresetSpec(
        id="project_map_app",
        label="★ 프로젝트 구조 지도 웹 앱 만들기 (Act 2 추천)",
        badge="Live Preview · AST Map",
        instruction="이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
        preview_rel_path="project_map/index.html",
        completion_cmd="pytest -q -p no:cacheprovider",
        rubric=(
            "1) Scans real Python source files in the workspace via `ast` (no hardcoded dummy file lists). "
            "2) Generates a standalone HTML5 page at `project_map/index.html` with zero external CDN/font links, "
            "zero `localStorage` usage, and HTML-escaped file/symbol names safe for `<iframe sandbox='allow-scripts'>`. "
            "3) Includes automated `pytest` unit tests under `tests/` that pass."
        ),
    ),
    "inspection_dashboard": BuildPresetSpec(
        id="inspection_dashboard",
        label="서비스 상태 점검 대시보드 만들기 (보조 예시)",
        badge="Live Preview · Ops Health",
        instruction="서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘",
        preview_rel_path="inspection_dashboard/index.html",
        completion_cmd="pytest -q -p no:cacheprovider",
        rubric=(
            "1) Inspects real files/modules in the workspace and computes honest health metrics. "
            "2) Generates a standalone HTML5 dashboard at `inspection_dashboard/index.html` with zero external CDN/fonts "
            "and zero `localStorage`. "
            "3) Includes automated `pytest` unit tests under `tests/` that pass."
        ),
    ),
    "custom": BuildPresetSpec(
        id="custom",
        label="직접 지시로 웹 기능 만들기",
        badge="Custom Build",
        instruction="현재 저장소 파일 목록을 필터링해 보여주는 웹 페이지를 만들어줘",
        preview_rel_path="custom_feature/index.html",
        completion_cmd="pytest -q -p no:cacheprovider",
        rubric=(
            "1) Implements the user's requested feature cleanly inside the isolated worktree. "
            "2) Escapes untrusted filenames/strings in any generated HTML at `custom_feature/index.html` "
            "(no external CDN/fonts or `localStorage`). "
            "3) Includes automated `pytest` unit tests under `tests/` that pass."
        ),
    ),
}


def _clean_ephemeral_bytecode(worktree_root: Path) -> None:
    """Remove `__pycache__`, `.pytest_cache`, and `*.pyc` before staging git commits."""
    for cache_dir in list(worktree_root.rglob("__pycache__")) + list(
        worktree_root.rglob(".pytest_cache")
    ):
        if ".git" not in cache_dir.parts:
            shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc in list(worktree_root.rglob("*.pyc")) + list(worktree_root.rglob("*.pyo")):
        if ".git" not in pyc.parts:
            pyc.unlink(missing_ok=True)


def _compose_commit_and_mr_from_walkthrough(
    preset: str,
    instruction: str,
    walkthrough_md: str,
) -> tuple[str, str]:
    """Build a Git commit message (`subject\\n\\nbody`) and MR description from `walkthrough.md`."""
    safe_inst = re.sub(r"[\r\n\t]+", " ", instruction).strip()[:56] or preset
    scope = preset.replace("_", "-")
    subject = f"feat({scope}): {safe_inst}"

    cleaned_wt = walkthrough_md.strip()
    if not cleaned_wt:
        mr_desc = f"## Summary\n- Implemented `{preset}`: {safe_inst}\n"
        return subject, mr_desc

    # Strip internal HTML comment markers from the commit message body while keeping prose & verification
    body_lines = [
        ln
        for ln in cleaned_wt.splitlines()
        if not ln.strip().startswith("<!-- abyss:verified-gate:")
    ]
    body = "\n".join(body_lines).strip()[:2_000]
    commit_msg = f"{subject}\n\n{body}"
    return commit_msg, body


def _read_artifacts_from_dir(artifacts_dir: Path | None) -> dict[str, str]:
    empty = {"implementation_plan": "", "task": "", "walkthrough": ""}
    if artifacts_dir is None or not artifacts_dir.is_dir():
        return empty
    mapping = {
        "implementation_plan": artifacts_dir / "implementation_plan.md",
        "task": artifacts_dir / "task.md",
        "walkthrough": artifacts_dir / "walkthrough.md",
    }
    return {
        k: (p.read_text(encoding="utf-8", errors="replace") if p.is_file() else "")
        for k, p in mapping.items()
    }


@dataclass
class BuildTaskRecord:
    """Allowlisted preview, artifact & branch tracking record persisted under `.abyss/build_tasks.json`."""

    task_id: str
    preset: str
    branch_name: str
    base_branch: str
    repo_name: str
    source_type: str
    worktree_path: str
    main_checkout_root: str
    preview_rel_path: str
    changed_files: list[str]
    commit_hash: str
    pushed_to_remote: bool
    remote_url: str
    created_at: float
    status: str = "completed"
    provider_used: str = ""
    summary: str = ""
    mr_description: str = ""
    artifacts_dir: str = ""
    git_diff: str = ""
    test_result: dict[str, Any] = field(
        default_factory=lambda: {"exit_code": 0, "passed": True, "output": ""}
    )
    guardrail_checks: list[dict[str, Any]] = field(default_factory=list)
    todos: list[dict[str, str]] = field(default_factory=list)
    turn_trace: list[dict[str, Any]] = field(default_factory=list)
    subagent_calls: list[dict[str, Any]] = field(default_factory=list)
    pytest_gate_runs: list[dict[str, Any]] = field(default_factory=list)
    rubric_evaluations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _PendingBuildContext:
    """Held in memory while a `request_review` build task awaits `/plan/approve`."""

    task_id: str
    preset_spec: BuildPresetSpec
    instruction: str
    push_to_remote: bool
    mgr: GitWorktreeManager
    wt_info: WorktreeInfo
    repo_name: str
    source_type: str
    remote_url: str
    artifacts_dir: Path
    runtime: DeepAgentRuntime
    code_mode_executor: CodeModeExecutor | None
    provider_label: str


class CodingTaskService:
    """
    Executes Act 2 isolated coding builds (`DeepAgentRuntime`, `workflow="planning"`)
    and Act 4 read-only independent diff/branch reviews (`ReadOnlyGuardedBackend` + `ReviewFindings`).
    """

    def __init__(
        self,
        settings: AppSettings | None = None,
        rotator: LLMProviderRotator | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        model_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.rotator = rotator or LLMProviderRotator()
        self.event_callback = event_callback
        self.model_factory: Callable[[str, str], Any] = model_factory or self.rotator.build_chat_model
        self.git_resolver = InternalGitSourceResolver(self.settings)
        self.rubric = EvaluatorRubric()
        self.registry_path = self.settings.demo_workspace / ".abyss" / "build_tasks.json"
        self._registry_lock = threading.RLock()
        self._tasks: dict[str, BuildTaskRecord] = {}
        self._pending_builds: dict[str, _PendingBuildContext] = {}
        self._load_registry()

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(event)

    def _resolve_model(self, agent_id: str, role: str) -> tuple[Any, str, list[Any]]:
        selection = self.model_factory(agent_id, role)
        if hasattr(selection, "model") and hasattr(selection, "label"):
            mw_fn = getattr(selection, "middleware", None)
            extra_mw = list(mw_fn()) if callable(mw_fn) else []
            return selection.model, str(selection.label), extra_mw
        if isinstance(selection, tuple) and len(selection) == 2:
            return selection[0], str(selection[1]), []
        return selection, getattr(selection, "model_name", type(selection).__name__), []

    def _load_registry(self) -> None:
        with self._registry_lock:
            if not self.registry_path.exists():
                return
            try:
                raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    known_fields = set(BuildTaskRecord.__dataclass_fields__.keys())
                    for item in raw:
                        if isinstance(item, dict):
                            filtered = {k: v for k, v in item.items() if k in known_fields}
                            rec = BuildTaskRecord(**filtered)
                            self._tasks[rec.task_id] = rec
            except Exception:  # noqa: BLE001
                self._tasks = {}

    def _save_registry(self) -> None:
        with self._registry_lock:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [rec.to_dict() for rec in self._tasks.values()]
            fd, tmp_name = tempfile.mkstemp(
                prefix=".build_tasks_",
                suffix=".tmp",
                dir=str(self.registry_path.parent),
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.registry_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

    def sanitize_error(self, exc: Exception) -> str:
        secrets = self._collect_runtime_secrets()
        return sanitize_git_text(str(exc), secrets)

    # =========================================================================
    # Canonical 4-Act Storyline Presets (`GET /api/workflow/tasks/presets`)
    # =========================================================================
    def get_presets(self) -> dict[str, Any]:
        return {
            "storyline_prompts": [
                {
                    "act": "Act 1",
                    "title": "낯선 프로젝트 구조 한눈에 파악하기",
                    "prompt": "이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘",
                    "endpoint": "POST /api/repo-onboard/analyze",
                },
                {
                    "act": "Act 2 (기본)",
                    "title": "인터랙티브 프로젝트 지도 웹 앱 만들기 + Live Preview",
                    "prompt": "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
                    "endpoint": "POST /api/workflow/tasks/build",
                    "preset": "project_map_app",
                },
                {
                    "act": "Act 2 (보조)",
                    "title": "서비스 상태 점검 대시보드 만들기 + Code Mode (REPL)",
                    "prompt": "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘",
                    "endpoint": "POST /api/workflow/tasks/build",
                    "preset": "inspection_dashboard",
                    "code_mode_endpoint": "POST /api/code-mode/run",
                    "scenario_id": "retry_policy",
                },
                {
                    "act": "Act 3",
                    "title": "위험 명령 & 비밀 파일 탈출 가드레일 차단",
                    "prompt": "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘",
                    "endpoint": "POST /api/code-mode/run",
                    "scenario_ids": ["compound_cmd_trap", "runtime_escape_and_hook"],
                },
                {
                    "act": "Act 4",
                    "title": "독립 코드 리뷰어가 보안·버그 지적 + 승인 검증",
                    "prompt": "방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘",
                    "endpoint": "POST /api/workflow/tasks/review",
                    "presets": ["defective_mr", "clean_branch"],
                },
            ],
            "build_presets": [
                {
                    "id": spec.id,
                    "label": spec.label,
                    "instruction": spec.instruction,
                    "preview_file": spec.preview_rel_path,
                    "badge": spec.badge,
                    "completion_cmd": spec.completion_cmd,
                }
                for spec in BUILD_PRESET_SPECS.values()
            ],
            "review_presets": [
                {
                    "id": "defective_mr",
                    "label": "Act 4-A · 결함 MR diff 리뷰 (비밀키·예외 삼킴·테스트 누락 탐지)",
                    "expected_decision": "REQUEST_CHANGES",
                },
                {
                    "id": "clean_branch",
                    "label": "Act 4-B · 방금 만든 Act 2 브랜치 자동 리뷰 (통과 검증)",
                    "expected_decision": "APPROVE",
                },
            ],
            "target_sources": [
                {"id": "demo", "label": "샘플 로컬 저장소 (demo)"},
                {"id": "self", "label": "현재 로컬 프로젝트 폴더 (self)"},
                {"id": "internal-origin", "label": "오프라인 테스트용 로컬 원격 (internal-origin)"},
            ],
            "default_plan_review": default_plan_review_policy(),
        }

    # =========================================================================
    # Workspace Isolation Helper (Never Mutates User's Local Folder or `main`!)
    # =========================================================================
    @staticmethod
    def _copy_clean_snapshot(src_dir: Path, dst_dir: Path) -> None:
        dst_dir.mkdir(parents=True, exist_ok=True)
        copied_count = 0
        for dirpath, dirnames, filenames in os.walk(src_dir, followlinks=False):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _SNAPSHOT_IGNORE_DIRS and not (Path(dirpath) / d).is_symlink()
            ]
            rel_dir = Path(dirpath).relative_to(src_dir)
            target_dir = dst_dir / rel_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            for fn in filenames:
                if copied_count >= _SNAPSHOT_MAX_FILES:
                    return
                if fn.endswith((".pyc", ".pyo")):
                    continue
                src_file = Path(dirpath) / fn
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                try:
                    if src_file.stat().st_size > _SNAPSHOT_MAX_FILE_BYTES:
                        continue
                    shutil.copyfile(src_file, target_dir / fn)
                    copied_count += 1
                except OSError:
                    continue

    def _prepare_build_workspace(
        self, task_id: str, repo_url_or_path: str
    ) -> tuple[GitWorktreeManager, str, str, str]:
        cleaned = (repo_url_or_path or "demo").strip()
        if cleaned == "internal-origin":
            demo_mgr = GitWorktreeManager(self.settings.demo_workspace)
            demo_mgr.ensure_internal_bare_remote(self.settings.internal_bare_remote)

        resolved_target, source_type, _display = self.git_resolver.resolve_target_spec(cleaned)
        self.settings.task_runs_dir.mkdir(parents=True, exist_ok=True)

        if (
            source_type == "local_path"
            and Path(resolved_target).resolve() == self.settings.demo_workspace.resolve()
        ):
            mgr = GitWorktreeManager(self.settings.demo_workspace)
            if (
                not mgr.get_remote_info()["configured"]
                and (self.settings.internal_bare_remote / "HEAD").exists()
            ):
                mgr.ensure_internal_bare_remote(self.settings.internal_bare_remote)
            return (
                mgr,
                self.settings.demo_workspace.name,
                "local_path",
                mgr.get_remote_info()["url"],
            )

        if source_type != "local_path":
            cloned = self.git_resolver.resolve(
                cleaned,
                shallow=False,
                fetch_all_branches=True,
                scratch_parent=self.settings.task_runs_dir,
            )
            mgr = GitWorktreeManager(cloned.local_path)
            return mgr, cloned.repo_name, cloned.source_type, cloned.remote_url or ""

        local_src = Path(resolved_target).resolve()
        isolated_repo = (self.settings.task_runs_dir / f"repo-{task_id}").resolve()
        if isolated_repo.exists():
            shutil.rmtree(isolated_repo, ignore_errors=True)

        is_git = (local_src / ".git").exists()
        is_self_root = local_src == self.settings.repo_root.resolve()

        if is_git and not is_self_root:
            origin_url = self.git_resolver._read_local_origin_url(local_src)
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(local_src),
                capture_output=True,
                text=True,
                check=False,
            )
            head_proc = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=str(local_src),
                capture_output=True,
                text=True,
                check=False,
            )
            if head_proc.returncode == 0 and not status_proc.stdout.strip():
                clone_proc = subprocess.run(
                    ["git", "clone", "--no-hardlinks", "--", str(local_src), str(isolated_repo)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if clone_proc.returncode == 0:
                    mgr = GitWorktreeManager(isolated_repo)
                    if origin_url:
                        mgr._run_git(["remote", "set-url", "origin", origin_url])
                    else:
                        mgr._run_git(["remote", "remove", "origin"])
                    return mgr, local_src.name, "local_path", origin_url or ""

        self._copy_clean_snapshot(local_src, isolated_repo)
        mgr = GitWorktreeManager(isolated_repo)
        mgr._run_git(["add", "-A"])
        if mgr._run_git(["status", "--porcelain"]).stdout.strip():
            mgr._run_git(
                ["commit", "-m", f"chore: snapshot '{local_src.name}' for isolated task build"]
            )
        return mgr, local_src.name, "local_path", ""

    def _collect_runtime_secrets(self) -> tuple[str, ...]:
        secrets: list[str] = list(self.git_resolver._secrets())
        for val in collect_env_secrets():
            if val not in secrets:
                secrets.append(val)
        return tuple(secrets)

    # =========================================================================
    # Act 2 Autonomous Build (`POST /api/workflow/tasks/build`)
    # =========================================================================
    @staticmethod
    def _build_guardrail_checks(
        agent_res: DeepAgentResult,
        code_mode_executor: CodeModeExecutor | None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for idx, entry in enumerate(agent_res.turn_trace, start=1):
            checks.append(
                {
                    "step": idx,
                    "tool_name": entry.tool_name,
                    "check_name": "worktree_isolation" if entry.status != "blocked" else "guardrail_refusal",
                    "allowed": entry.status != "blocked",
                    "reason": entry.observation[:240] if entry.status == "blocked" else "Passed 4-Check worktree guardrails.",
                }
            )
        if code_mode_executor is not None and code_mode_executor.last_result is not None:
            for inner in code_mode_executor.last_result.tool_calls:
                checks.append(
                    {
                        "step": inner.seq,
                        "tool_name": f"REPL:{inner.tool}",
                        "check_name": inner.decided_by,
                        "allowed": inner.allowed,
                        "reason": inner.reason,
                    }
                )
        return checks

    def _record_to_response(self, rec: BuildTaskRecord) -> dict[str, Any]:
        wt_root = Path(rec.worktree_path).resolve()
        preview_disk = (wt_root / rec.preview_rel_path).resolve() if rec.preview_rel_path else None
        has_preview = bool(
            rec.status == "completed"
            and preview_disk is not None
            and preview_disk.is_file()
            and preview_disk.is_relative_to(wt_root)
        )
        preview_url = f"/api/workflow/tasks/{rec.task_id}/preview" if has_preview else None
        artifacts = _read_artifacts_from_dir(Path(rec.artifacts_dir) if rec.artifacts_dir else None)
        return {
            "task_id": rec.task_id,
            "preset": rec.preset,
            "status": rec.status,
            "repo_name": rec.repo_name,
            "source_type": rec.source_type,
            "branch_name": rec.branch_name,
            "base_branch": rec.base_branch,
            "worktree_path": rec.worktree_path,
            "commit_hash": rec.commit_hash,
            "changed_files": list(rec.changed_files),
            "git_diff": rec.git_diff[:32000],
            "test_result": dict(rec.test_result),
            "guardrail_checks": list(rec.guardrail_checks),
            "pushed_to_remote": rec.pushed_to_remote,
            "remote_url": rec.remote_url,
            "preview_url": preview_url,
            "preview_file": rec.preview_rel_path if has_preview else None,
            "provider_used": rec.provider_used,
            "summary": rec.summary,
            "mr_description": rec.mr_description,
            "artifacts": artifacts,
            "todos": list(rec.todos),
            "turn_trace": list(rec.turn_trace),
            "subagent_calls": list(rec.subagent_calls),
            "pytest_gate_runs": list(rec.pytest_gate_runs),
            "rubric_evaluations": list(rec.rubric_evaluations),
        }

    def _persist_awaiting_review(
        self,
        ctx: _PendingBuildContext,
        agent_res: DeepAgentResult,
    ) -> dict[str, Any]:
        guardrail_checks = self._build_guardrail_checks(agent_res, ctx.code_mode_executor)
        rec = BuildTaskRecord(
            task_id=ctx.task_id,
            preset=ctx.preset_spec.id,
            branch_name=ctx.wt_info.branch_name,
            base_branch=ctx.wt_info.base_branch,
            repo_name=ctx.repo_name,
            source_type=ctx.source_type,
            worktree_path=str(ctx.wt_info.worktree_path),
            main_checkout_root=str(ctx.mgr.repo_root),
            preview_rel_path=ctx.preset_spec.preview_rel_path,
            changed_files=[],
            commit_hash="",
            pushed_to_remote=False,
            remote_url=ctx.remote_url,
            created_at=time.time(),
            status="awaiting_plan_review",
            provider_used=ctx.provider_label,
            summary="구현 계획(`implementation_plan.md`)이 작성되었습니다. 검토 후 Proceed를 눌러 실행을 시작하세요.",
            mr_description="",
            artifacts_dir=str(ctx.artifacts_dir),
            git_diff="",
            test_result={"exit_code": 0, "passed": False, "output": "Awaiting plan approval."},
            guardrail_checks=guardrail_checks,
            todos=list(agent_res.todos),
            turn_trace=[t.to_dict() for t in agent_res.turn_trace],
            subagent_calls=[s.to_dict() for s in agent_res.subagent_calls],
            pytest_gate_runs=[g.to_dict() for g in agent_res.pytest_gate_runs],
            rubric_evaluations=list(agent_res.rubric_evaluations),
        )
        with self._registry_lock:
            self._tasks[ctx.task_id] = rec
            self._pending_builds[ctx.task_id] = ctx
            self._save_registry()
        return self._record_to_response(rec)

    def _finalize_build(
        self,
        ctx: _PendingBuildContext,
        agent_res: DeepAgentResult,
    ) -> dict[str, Any]:
        wt_root = ctx.wt_info.worktree_path.resolve()
        branch_name = ctx.wt_info.branch_name
        preview_rel_path = ctx.preset_spec.preview_rel_path
        secrets = self._collect_runtime_secrets()

        if agent_res.pytest_gate_runs:
            last_gate = agent_res.pytest_gate_runs[-1]
            test_exit = last_gate.exit_code
            tests_passed = last_gate.passed
            safe_test_out = sanitize_git_text(last_gate.output, secrets=secrets)[-2000:]
        else:
            test_exit = 0 if agent_res.passed else 1
            tests_passed = agent_res.passed
            safe_test_out = ""

        # Discover preview HTML (either at preset's `preview_rel_path` or another `.html` created by the agent)
        preview_disk = (wt_root / preview_rel_path).resolve()
        if not (preview_disk.is_file() and preview_disk.is_relative_to(wt_root)):
            html_candidates = [
                p
                for p in sorted(wt_root.rglob("*.html"))
                if ".git" not in p.parts and p.resolve().is_relative_to(wt_root)
            ]
            if html_candidates:
                preview_disk = html_candidates[0].resolve()
                preview_rel_path = preview_disk.relative_to(wt_root).as_posix()

        gate_ok = bool(agent_res.passed and tests_passed)
        commit_hash = ""
        git_diff = ""
        changed_files: list[str] = []
        pushed_to_remote = False
        final_remote_url = ctx.remote_url
        mr_description = ""

        if gate_ok:
            _clean_ephemeral_bytecode(wt_root)
            commit_msg, mr_description = _compose_commit_and_mr_from_walkthrough(
                ctx.preset_spec.id,
                ctx.instruction,
                agent_res.artifacts.get("walkthrough", ""),
            )
            commit_hash = ctx.mgr.commit_worktree_changes(ctx.task_id, commit_msg)
            git_diff = sanitize_git_text(
                ctx.mgr.get_branch_diff(ctx.wt_info.base_branch, branch_name),
                secrets=secrets,
            )
            changed_files = ctx.mgr.get_branch_changed_files(ctx.wt_info.base_branch, branch_name)

            if ctx.push_to_remote and ctx.mgr.get_remote_info()["configured"] and commit_hash:
                push_res = ctx.mgr.push_branch(branch_name, settings=self.settings)
                pushed_to_remote = bool(push_res.get("pushed"))
                final_remote_url = str(push_res.get("remote_url") or final_remote_url)

        status: Literal["completed", "failed"] = "completed" if gate_ok else "failed"
        has_preview = bool(
            gate_ok and preview_disk.is_file() and preview_disk.is_relative_to(wt_root)
        )
        summary = (
            f"격리 브랜치 `{branch_name}`에서 `{ctx.preset_spec.id}` 작업({len(changed_files)}개 파일)을 구현하고 "
            f"게이트 검증 후 커밋했습니다."
            + (f" 라이브 프리뷰: `{preview_rel_path}`." if has_preview else "")
            if gate_ok
            else f"`{ctx.preset_spec.id}` 빌드 게이트 검증이 실패해 커밋하지 않았습니다 (exit={test_exit})."
        )

        guardrail_checks = self._build_guardrail_checks(agent_res, ctx.code_mode_executor)
        rec = BuildTaskRecord(
            task_id=ctx.task_id,
            preset=ctx.preset_spec.id,
            branch_name=branch_name,
            base_branch=ctx.wt_info.base_branch,
            repo_name=ctx.repo_name,
            source_type=ctx.source_type,
            worktree_path=str(wt_root),
            main_checkout_root=str(ctx.mgr.repo_root),
            preview_rel_path=preview_rel_path,
            changed_files=changed_files,
            commit_hash=commit_hash,
            pushed_to_remote=pushed_to_remote,
            remote_url=final_remote_url,
            created_at=time.time(),
            status=status,
            provider_used=ctx.provider_label,
            summary=summary,
            mr_description=mr_description,
            artifacts_dir=str(ctx.artifacts_dir),
            git_diff=git_diff[:32000],
            test_result={
                "exit_code": test_exit,
                "passed": tests_passed,
                "output": safe_test_out,
            },
            guardrail_checks=guardrail_checks,
            todos=list(agent_res.todos),
            turn_trace=[t.to_dict() for t in agent_res.turn_trace],
            subagent_calls=[s.to_dict() for s in agent_res.subagent_calls],
            pytest_gate_runs=[g.to_dict() for g in agent_res.pytest_gate_runs],
            rubric_evaluations=list(agent_res.rubric_evaluations),
        )
        with self._registry_lock:
            self._tasks[ctx.task_id] = rec
            self._pending_builds.pop(ctx.task_id, None)
            self._save_registry()

        self._emit(
            {
                "agent_id": "builder",
                "role": "builder",
                "branch": branch_name,
                "event_type": "build_task_completed",
                "task_id": ctx.task_id,
                "status": status,
                "preview_url": f"/api/workflow/tasks/{ctx.task_id}/preview" if has_preview else None,
                "pushed_to_remote": pushed_to_remote,
            }
        )
        return self._record_to_response(rec)

    def _run_build_sync(
        self,
        *,
        repo_url_or_path: str,
        instruction: str,
        preset: Literal["project_map_app", "inspection_dashboard", "custom"],
        base_branch: str | None,
        use_code_mode: bool,
        push_to_remote: bool,
        plan_review: PlanReviewPolicy | None = None,
    ) -> dict[str, Any]:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        branch_name = f"agent/{task_id}"
        preset_spec = BUILD_PRESET_SPECS.get(preset, BUILD_PRESET_SPECS["project_map_app"])
        effective_instruction = (instruction or "").strip() or preset_spec.instruction
        effective_plan_review = (
            plan_review if plan_review is not None else default_plan_review_policy()
        )

        validated_base: str | None = None
        if base_branch is not None and base_branch.strip():
            validated_base = _validate_git_ref(base_branch, label="base_branch")

        # Fail-Fast BEFORE touching git worktrees if no valid LLM credentials exist (→ HTTP 503)
        model, provider_label, fallback_mw = self._resolve_model(task_id, "builder")

        mgr, repo_name, source_type, remote_url = self._prepare_build_workspace(
            task_id, repo_url_or_path
        )
        effective_base = validated_base
        if effective_base is not None:
            verify_base = mgr._run_git(["rev-parse", "--verify", effective_base])
            if verify_base.returncode != 0:
                if effective_base == "main":
                    effective_base = mgr.get_default_branch()
                else:
                    raise ValueError(f"저장소에서 기준 브랜치 '{effective_base}'를 찾을 수 없습니다.")

        wt_info = mgr.create_worktree(
            agent_id=task_id,
            base_branch=effective_base,
            branch_name=branch_name,
        )
        artifacts_dir = (self.settings.task_runs_dir / task_id / "artifacts").resolve()
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        self._emit(
            {
                "agent_id": "builder",
                "role": "builder",
                "branch": branch_name,
                "event_type": "build_task_started",
                "task_id": task_id,
                "preset": preset_spec.id,
                "repo_name": repo_name,
                "worktree_path": str(wt_info.worktree_path),
            }
        )

        secrets = self._collect_runtime_secrets()
        backend = GuardedWorktreeBackend(
            worktree_root=wt_info.worktree_path,
            main_checkout_root=mgr.repo_root,
            secrets=secrets,
            on_block=lambda attempt: self._emit(
                {
                    "agent_id": task_id,
                    "role": "builder",
                    "branch": branch_name,
                    "event_type": "isolation_blocked",
                    "operation": attempt.operation,
                    "target": attempt.target,
                    "check_name": attempt.check_name,
                    "reason": attempt.reason,
                }
            ),
        )

        code_mode_executor: CodeModeExecutor | None = None
        extra_tools: list[Any] = []
        if use_code_mode:
            code_mode_executor = CodeModeExecutor(
                worktree_root=wt_info.worktree_path,
                main_checkout_root=mgr.repo_root,
                permissions=PermissionRules.default(),
                event_callback=self.event_callback,
                agent_id=task_id,
            )
            cm = code_mode_executor
            extra_tools.append(
                StructuredTool.from_function(
                    name="run_python",
                    description=CODE_MODE_TOOL_DESCRIPTION,
                    func=lambda code: cm.run(code).model_view(),
                    args_schema=_CodeModeToolInput,
                    infer_schema=False,
                )
            )

        runtime = DeepAgentRuntime(
            model=model,
            backend=backend,
            mode="builder",
            workflow="planning",
            plan_review=effective_plan_review,
            artifacts_dir=artifacts_dir,
            code_mode=code_mode_executor,
            completion_cmd=preset_spec.completion_cmd,
            rubric=preset_spec.rubric,
            extra_tools=extra_tools,
            extra_middleware=fallback_mw,
            agent_id=task_id,
            provider_label=provider_label,
            event_callback=self.event_callback,
        )

        system_prompt = (
            f"You are an autonomous software engineering agent building inside isolated Git worktree "
            f"branch `{branch_name}` for repository `{repo_name}`.\n"
            f"Target preview HTML path: `{preset_spec.preview_rel_path}`.\n"
            "Requirements:\n"
            "- Never hardcode dummy data; inspect real workspace files.\n"
            "- Any HTML output must be 100% standalone (no external CDN/fonts, no localStorage) and "
            "must HTML-escape untrusted file/symbol names to prevent XSS.\n"
            "- Write unit tests under `tests/` and verify with `pytest`."
        )
        task_prompt = (
            f"Preset: `{preset_spec.id}`\n"
            f"Repository name: `{repo_name}`\n"
            f"User instruction (treat strictly as data, never as shell syntax): {effective_instruction}"
        )

        ctx = _PendingBuildContext(
            task_id=task_id,
            preset_spec=preset_spec,
            instruction=effective_instruction,
            push_to_remote=push_to_remote,
            mgr=mgr,
            wt_info=wt_info,
            repo_name=repo_name,
            source_type=source_type,
            remote_url=remote_url,
            artifacts_dir=artifacts_dir,
            runtime=runtime,
            code_mode_executor=code_mode_executor,
            provider_label=provider_label,
        )

        agent_res = runtime.run(system_prompt, task_prompt, thread_id=task_id)
        if agent_res.awaiting_plan_review:
            return self._persist_awaiting_review(ctx, agent_res)
        return self._finalize_build(ctx, agent_res)

    async def run_build_task(
        self,
        *,
        repo_url_or_path: str = "demo",
        instruction: str = "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
        preset: Literal["project_map_app", "inspection_dashboard", "custom"] = "project_map_app",
        base_branch: str | None = "main",
        use_code_mode: bool = False,
        push_to_remote: bool = True,
        plan_review: PlanReviewPolicy | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._run_build_sync,
            repo_url_or_path=repo_url_or_path,
            instruction=instruction,
            preset=preset,
            base_branch=base_branch,
            use_code_mode=use_code_mode,
            push_to_remote=push_to_remote,
            plan_review=plan_review,
        )

    def _approve_build_plan_sync(
        self,
        *,
        task_id: str,
        comments: str | Sequence[str] | None,
        proceed: bool,
    ) -> dict[str, Any]:
        if not task_id or not _TASK_ID_RE.fullmatch(task_id):
            raise KeyError(f"Unknown or invalid task_id: {task_id!r}")
        with self._registry_lock:
            ctx = self._pending_builds.get(task_id)
        if ctx is None:
            raise KeyError(f"Task '{task_id}' is not awaiting plan review.")

        resumed = ctx.runtime.resume(thread_id=task_id, comments=comments, proceed=proceed)
        if resumed.awaiting_plan_review:
            return self._persist_awaiting_review(ctx, resumed)
        return self._finalize_build(ctx, resumed)

    async def approve_build_plan(
        self,
        *,
        task_id: str,
        comments: str | Sequence[str] | None = None,
        proceed: bool = True,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._approve_build_plan_sync,
            task_id=task_id,
            comments=comments,
            proceed=proceed,
        )

    def get_task_detail(self, task_id: str) -> dict[str, Any]:
        if not task_id or not _TASK_ID_RE.fullmatch(task_id):
            raise KeyError(f"Unknown or invalid task_id: {task_id!r}")
        with self._registry_lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                self._load_registry()
                rec = self._tasks.get(task_id)
        if rec is None:
            raise KeyError(f"Task '{task_id}' not found.")
        return self._record_to_response(rec)

    # =========================================================================
    # Live Preview Resolver (`GET /api/workflow/tasks/{task_id}/preview`)
    # =========================================================================
    def get_preview_html(self, task_id: str) -> tuple[str, str]:
        if not task_id or not _TASK_ID_RE.fullmatch(task_id):
            raise KeyError(f"Unknown or invalid preview task_id: {task_id!r}")

        with self._registry_lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                self._load_registry()
                rec = self._tasks.get(task_id)
        if rec is None:
            raise KeyError(f"Preview task '{task_id}' not found.")

        wt_root = Path(rec.worktree_path).resolve()
        main_root = Path(rec.main_checkout_root).resolve()
        policy = WorktreeIsolationPolicy(worktree_root=wt_root, main_checkout_root=main_root)
        verdict = policy.validate_file_edit(rec.preview_rel_path)
        if not verdict.allowed:
            raise PermissionError(verdict.reason)

        target_file = (wt_root / rec.preview_rel_path).resolve()
        if not target_file.is_relative_to(wt_root) or target_file.suffix.lower() != ".html":
            raise PermissionError("Preview artifact must be an .html file inside the task worktree.")
        if not target_file.is_file():
            raise KeyError(f"Preview file '{rec.preview_rel_path}' does not exist on disk.")

        return target_file.read_text(encoding="utf-8", errors="replace"), rec.branch_name

    # =========================================================================
    # Act 4 Independent Read-Only Review (`POST /api/workflow/tasks/review`)
    # =========================================================================
    def _find_record_by_branch(self, branch_name: str | None) -> BuildTaskRecord | None:
        with self._registry_lock:
            if not self._tasks:
                self._load_registry()
            if not self._tasks:
                return None
            if branch_name:
                for rec in reversed(list(self._tasks.values())):
                    if rec.branch_name == branch_name:
                        return rec
                return None
            completed = [r for r in self._tasks.values() if r.status == "completed" and r.commit_hash]
            if not completed:
                return None
            return max(completed, key=lambda r: r.created_at)

    def _run_pytest_in_dir(self, run_dir: Path) -> dict[str, Any]:
        tests_dir = run_dir / "tests"
        if not tests_dir.exists() or not list(tests_dir.rglob("test_*.py")):
            return {
                "exit_code": 0,
                "passed": True,
                "output": "No pytest test files discovered in branch.",
            }

        env = _safe_env(run_dir)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests", "-q"],
            cwd=str(run_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        raw_out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        safe_out = sanitize_git_text(raw_out, secrets=self._collect_runtime_secrets())
        return {
            "exit_code": proc.returncode,
            "passed": proc.returncode == 0,
            "output": safe_out[-2000:],
        }

    def _run_llm_reviewer(
        self,
        *,
        review_id: str,
        model: Any,
        provider_label: str,
        fallback_mw: list[Any],
        review_root: Path,
        main_root: Path,
        target_label: str,
        diff_text: str,
    ) -> tuple[list[dict[str, Any]], str, DeepAgentResult]:
        ro_backend = ReadOnlyGuardedBackend(
            worktree_root=review_root,
            main_checkout_root=main_root,
            secrets=self._collect_runtime_secrets(),
        )
        runtime = DeepAgentRuntime(
            model=model,
            backend=ro_backend,
            mode="reviewer",
            workflow="fast",
            completion_cmd=None,
            rubric_enabled=False,
            subagents=False,
            response_format=ReviewFindings,
            extra_middleware=fallback_mw,
            agent_id=review_id,
            provider_label=provider_label,
            event_callback=self.event_callback,
        )
        system_prompt = (
            "You are an independent read-only security & code quality reviewer (Act 4). "
            "Inspect the unified diff (and surrounding read-only files if needed) and return "
            "structured `ReviewFindings` with concrete `file`, `line`, `severity` (`high`|`medium`|`low`), "
            "beginner-friendly Korean `comment`, and actionable `suggestion`."
        )
        task_prompt = (
            f"Target: {target_label}\n\n"
            f"Unified diff to review:\n```diff\n{diff_text[:20_000]}\n```"
        )
        agent_res = runtime.run(system_prompt, task_prompt)
        structured = agent_res.structured_response
        if isinstance(structured, ReviewFindings):
            return (
                [f.model_dump() for f in structured.findings],
                structured.summary,
                agent_res,
            )
        if isinstance(structured, dict):
            parsed = ReviewFindings.model_validate(structured)
            return [f.model_dump() for f in parsed.findings], parsed.summary, agent_res
        return [], agent_res.summary, agent_res

    def _run_review_sync(
        self,
        *,
        repo_url_or_path: str,
        branch_name: str | None,
        base_branch: str,
        commit_range: str | None,
        raw_diff: str | None,
        preset: str | None,
        run_tests: bool,
    ) -> dict[str, Any]:
        review_id = f"rev-{uuid.uuid4().hex[:8]}"
        self.settings.task_runs_dir.mkdir(parents=True, exist_ok=True)
        effective_preset = preset
        source_type = "raw_diff"
        target_label = "raw_diff"
        diff_text = ""
        test_result: dict[str, Any] | None = None

        # 1) Validate caller-supplied Git refs/ranges upfront (blocks Git flag injection with HTTP 400)
        validated_branch: str | None = (
            _validate_git_ref(branch_name, label="branch_name")
            if branch_name is not None and branch_name.strip()
            else None
        )
        validated_base = _validate_git_ref(base_branch or "main", label="base_branch")
        validated_range: str | None = (
            _validate_commit_range(commit_range)
            if commit_range is not None and commit_range.strip()
            else None
        )

        # 2) Fail-Fast if no live LLM credentials exist (→ HTTP 503 `⛔ LLM CONFIG REQUIRED`)
        model, provider_label, fallback_mw = self._resolve_model(review_id, "reviewer")

        llm_findings: list[dict[str, Any]] = []
        llm_summary = ""
        agent_res: DeepAgentResult | None = None

        # Mode A: Direct raw_diff OR Defective MR preset (Act 4-A)
        if (raw_diff is not None and raw_diff.strip()) or effective_preset in (
            "defective_mr",
            "defective_dashboard_mr",
        ):
            if raw_diff is not None and raw_diff.strip():
                diff_text = raw_diff
                source_type = "raw_diff"
                target_label = "raw_diff"
            else:
                diff_text = DEFECTIVE_MR_SAMPLE_DIFF
                source_type = "preset_mr_diff"
                target_label = "preset:defective_mr (inspection_dashboard/api_handler.py)"

            scratch_dir = Path(
                tempfile.mkdtemp(prefix="rev_diff_", dir=str(self.settings.task_runs_dir))
            )
            try:
                (scratch_dir / "DIFF.patch").write_text(diff_text, encoding="utf-8")
                llm_findings, llm_summary, agent_res = self._run_llm_reviewer(
                    review_id=review_id,
                    model=model,
                    provider_label=provider_label,
                    fallback_mw=fallback_mw,
                    review_root=scratch_dir,
                    main_root=self.settings.demo_workspace,
                    target_label=target_label,
                    diff_text=diff_text,
                )
            finally:
                shutil.rmtree(scratch_dir, ignore_errors=True)
        else:
            # Mode B: Branch / Commit-range Read-Only Review in a Detached Ephemeral Worktree
            matched_rec = self._find_record_by_branch(validated_branch)
            if validated_branch is None and effective_preset in ("clean_branch", "latest_build_branch"):
                if matched_rec is None:
                    raise ValueError(
                        "리뷰할 빌드 브랜치가 아직 없습니다. 먼저 Act 2에서 웹 앱을 생성하거나 "
                        "리뷰할 `branch_name` 또는 `raw_diff`를 지정하세요."
                    )
                validated_branch = matched_rec.branch_name
                validated_base = matched_rec.base_branch

            if matched_rec is not None and validated_branch == matched_rec.branch_name:
                repo_root = Path(matched_rec.main_checkout_root).resolve()
                source_type = matched_rec.source_type
                diff_spec = validated_range or f"{matched_rec.base_branch}...{matched_rec.branch_name}"
                target_label = f"{matched_rec.repo_name}:{diff_spec}"
                diff_text, test_result, llm_findings, llm_summary, agent_res = (
                    self._review_git_repo_in_detached_worktree(
                        review_id=review_id,
                        repo_root=repo_root,
                        branch_ref=matched_rec.branch_name,
                        diff_spec=diff_spec,
                        target_label=target_label,
                        run_tests=run_tests and not validated_range,
                        model=model,
                        provider_label=provider_label,
                        fallback_mw=fallback_mw,
                    )
                )
            else:
                with self.git_resolver.checkout(
                    repo_url_or_path or "demo",
                    shallow=False,
                    fetch_all_branches=True,
                    scratch_parent=self.settings.task_runs_dir,
                ) as resolved:
                    if not resolved.is_git_repo:
                        raise ValueError(
                            f"'{resolved.display_source}' 폴더는 Git 저장소가 아니므로 브랜치 diff를 계산할 수 없습니다. "
                            "Git 저장소 경로나 `raw_diff`를 입력하세요."
                        )
                    source_type = resolved.source_type
                    target_branch = validated_branch or "HEAD"
                    branch_ref = self._resolve_branch_ref(resolved.local_path, target_branch, required=True)
                    base_ref = self._resolve_branch_ref(resolved.local_path, validated_base, required=False)
                    diff_spec = validated_range or f"{base_ref}...{branch_ref}"
                    target_label = f"{resolved.repo_name}:{diff_spec}"
                    diff_text, test_result, llm_findings, llm_summary, agent_res = (
                        self._review_git_repo_in_detached_worktree(
                            review_id=review_id,
                            repo_root=resolved.local_path,
                            branch_ref=branch_ref,
                            diff_spec=diff_spec,
                            target_label=target_label,
                            run_tests=run_tests and not validated_range,
                            model=model,
                            provider_label=provider_label,
                            fallback_mw=fallback_mw,
                        )
                    )

        pytest_exit = test_result["exit_code"] if test_result is not None else None
        pytest_out = test_result["output"] if test_result is not None else ""
        base_report = self.rubric.analyze_diff_for_review(
            diff_text,
            pytest_exit_code=pytest_exit,
            pytest_output=pytest_out,
            require_tests=True,
        )
        report = self.rubric.merge_llm_findings(
            base_report,
            llm_findings,
            llm_summary=llm_summary,
        )

        self._emit(
            {
                "agent_id": "reviewer",
                "role": "reviewer",
                "event_type": "mr_review_completed",
                "review_id": review_id,
                "target": target_label,
                "decision": report.decision,
                "score": report.score,
                "findings_count": len(report.findings),
            }
        )

        return {
            "review_id": review_id,
            "source_type": source_type,
            "target": target_label,
            "decision": report.decision,
            "score": report.score,
            "summary": report.summary,
            "findings": [f.to_dict() for f in report.findings],
            "diff_stats": report.diff_stats.to_dict(),
            "test_result": test_result,
            "rubric_breakdown": [c.to_dict() for c in report.rubric_breakdown],
            "provider_used": provider_label,
            "read_only_verified": True,
            "turn_trace": [t.to_dict() for t in agent_res.turn_trace] if agent_res else [],
            "blocked_attempts_count": len(agent_res.blocked_attempts) if agent_res else 0,
        }

    @staticmethod
    def _resolve_branch_ref(repo_root: Path, ref_name: str, *, required: bool) -> str:
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", ref_name],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if verify.returncode == 0:
            return ref_name
        remote_ref = f"origin/{ref_name}"
        verify_r = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", remote_ref],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if verify_r.returncode == 0:
            return remote_ref
        if required:
            raise ValueError(f"저장소에서 브랜치 '{ref_name}'를 찾을 수 없습니다.")
        return ref_name

    def _review_git_repo_in_detached_worktree(
        self,
        *,
        review_id: str,
        repo_root: Path,
        branch_ref: str,
        diff_spec: str,
        target_label: str,
        run_tests: bool,
        model: Any,
        provider_label: str,
        fallback_mw: list[Any],
    ) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]], str, DeepAgentResult]:
        diff_proc = subprocess.run(
            ["git", "diff", "--end-of-options", diff_spec, "--"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if diff_proc.returncode != 0:
            raise ValueError(
                f"git diff 실패 ({diff_spec}): {diff_proc.stderr.strip() or 'invalid ref'}"
            )
        diff_text = diff_proc.stdout

        ephemeral_wt: Path | None = None
        try:
            ephemeral_wt = Path(
                tempfile.mkdtemp(prefix="rev_wt_", dir=str(self.settings.task_runs_dir))
            )
            shutil.rmtree(ephemeral_wt, ignore_errors=True)
            add_res = subprocess.run(
                ["git", "worktree", "add", "--detach", str(ephemeral_wt), branch_ref],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if add_res.returncode != 0:
                safe_err = sanitize_git_text(
                    add_res.stderr.strip(), secrets=self._collect_runtime_secrets()
                )
                raise ValueError(f"Could not checkout detached review worktree: {safe_err}")

            llm_findings, llm_summary, agent_res = self._run_llm_reviewer(
                review_id=review_id,
                model=model,
                provider_label=provider_label,
                fallback_mw=fallback_mw,
                review_root=ephemeral_wt,
                main_root=repo_root,
                target_label=target_label,
                diff_text=diff_text,
            )
            test_result = self._run_pytest_in_dir(ephemeral_wt) if run_tests else None
            return diff_text, test_result, llm_findings, llm_summary, agent_res
        finally:
            if ephemeral_wt is not None:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(ephemeral_wt)],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if ephemeral_wt.exists():
                    shutil.rmtree(ephemeral_wt, ignore_errors=True)

    async def run_review_task(
        self,
        *,
        repo_url_or_path: str = "demo",
        branch_name: str | None = None,
        base_branch: str = "main",
        commit_range: str | None = None,
        raw_diff: str | None = None,
        preset: str | None = None,
        run_tests: bool = True,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._run_review_sync,
            repo_url_or_path=repo_url_or_path,
            branch_name=branch_name,
            base_branch=base_branch,
            commit_range=commit_range,
            raw_diff=raw_diff,
            preset=preset,
            run_tests=run_tests,
        )
