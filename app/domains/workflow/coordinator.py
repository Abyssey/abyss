"""
app/domains/workflow/coordinator.py
Multi-Agent Parallel Worktree Coordinator powered by `langchain-ai/deepagents`.

Pipeline:
  1. Lead Agent (`DeepAgentRuntime(mode="direct", response_format=TeamPlan)`):
     Decomposes `user_goal` into 2–3 isolated `TeamTask` slices, tracks `write_todos`
     progress, and writes a team-level `implementation_plan.md`.
  2. Parallel Worktree Workers (`DeepAgentRuntime(mode="worker", workflow="planning")`):
     Each worker operates inside its own locked Git worktree (`.abyss/worktrees/<worker_id>`)
     behind `GuardedWorktreeBackend`, exchanges contracts via Agent Teams Mailbox tools
     (`send_team_message`, `read_team_messages`, `update_task_status`), and produces its
     own `implementation_plan.md`, `task.md`, and stamped `walkthrough.md`.
  3. Independent Reviewer Gate (`EvaluatorRubric`):
     Scores each worker branch on automated pytest, diff substance, symbol contracts, and hygiene.
  4. Lead Sequential Merge & Team Walkthrough:
     Merges approved branches onto `main` (`git merge --no-ff`), runs integration `pytest`,
     pushes `main` when an internal remote is configured, and synthesizes team `walkthrough.md`.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.core.config import AppSettings
from app.core.llm.provider_rotator import LLMProviderRotator
from app.domains.execution.deepagent_backend import (
    GuardedWorktreeBackend,
    ReadOnlyGuardedBackend,
    _safe_env,
    collect_env_secrets,
)
from app.domains.execution.runtime import DeepAgentResult, DeepAgentRuntime
from app.domains.workflow.evaluator import EvaluatorRubric, ReviewDecision
from app.domains.workflow.worktree_adapter import GitWorktreeManager


class TeamTaskSpec(BaseModel):
    """Single parallelizable slice emitted by the Lead `DeepAgentRuntime`."""

    task_id: str = Field(default="", description="Identifier such as TASK-1, TASK-2, TASK-3.")
    title: str = Field(description="Concise engineering title of the slice.")
    description: str = Field(description="Actionable implementation specification for the worker.")
    assigned_to: str = Field(
        default="",
        description="Worker roster ID (e.g., worker-analyzer, worker-tutor, worker-web).",
    )
    target_files: list[str] = Field(
        default_factory=list,
        description="Relative file paths inside the worktree owned by this worker (code + tests).",
    )
    required_symbols: list[str] = Field(
        default_factory=list,
        description="Key class/function names that must appear in the diff.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Optional upstream task_ids whose branches must merge before this worker runs.",
    )


class TeamPlan(BaseModel):
    """Structured output returned by the Lead `DeepAgentRuntime`."""

    summary: str = Field(description="Architectural summary of how the goal is split across workers.")
    tasks: list[TeamTaskSpec] = Field(
        min_length=1,
        max_length=4,
        description="2–3 isolated worktree tasks with non-overlapping target_files.",
    )


class _SendMailboxInput(BaseModel):
    recipient: str = Field(description="Target teammate ID (e.g., worker-tutor, worker-web, lead).")
    message: str = Field(description="Schema contract or coordination note to deliver.")


class _EmptyToolInput(BaseModel):
    pass


class _UpdateTaskStatusInput(BaseModel):
    task_id: str = Field(description="Shared task board ID (e.g., TASK-1).")
    status: str = Field(description="New status (in_progress, in_review).")
    note: str = Field(default="", description="Short progress note.")


@dataclass
class TeamTask:
    """공유 태스크 보드(.abyss/tasks.json)의 개별 작업 항목."""

    task_id: str
    title: str
    description: str
    assigned_to: str
    worktree_branch: str
    target_files: list[str]
    required_symbols: list[str]
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    review_score: int = 0
    commit_hash: str = ""
    provider_used: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "assigned_to": self.assigned_to,
            "worktree_branch": self.worktree_branch,
            "target_files": self.target_files,
            "required_symbols": self.required_symbols,
            "depends_on": self.depends_on,
            "status": self.status,
            "review_score": self.review_score,
            "commit_hash": self.commit_hash,
            "provider_used": self.provider_used,
        }


@dataclass
class MailboxMessage:
    """에이전트 팀 메일박스(.abyss/mailbox/*.jsonl) 메시지."""

    msg_id: str
    timestamp: float
    sender: str
    recipient: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
        }


_DEFAULT_WORKER_ROSTER = ("worker-analyzer", "worker-tutor", "worker-web")


def _clean_ephemeral_bytecode(worktree_root: Path) -> None:
    for cache_dir in list(worktree_root.rglob("__pycache__")) + list(
        worktree_root.rglob(".pytest_cache")
    ):
        if ".git" not in cache_dir.parts:
            shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc in list(worktree_root.rglob("*.pyc")) + list(worktree_root.rglob("*.pyo")):
        if ".git" not in pyc.parts:
            pyc.unlink(missing_ok=True)


def _render_team_plan_markdown(goal: str, summary: str, tasks: list[TeamTask]) -> str:
    lines = [
        "# Team Implementation Plan",
        "",
        "## Goal",
        goal,
        "",
        "## Architectural Decomposition",
        summary or "Parallel worktree decomposition produced by Lead DeepAgent.",
        "",
        "## Assigned Worktree Slices",
    ]
    for t in tasks:
        files_str = ", ".join(f"`{f}`" for f in t.target_files) or "(unscoped)"
        deps_str = f" (depends on: {', '.join(t.depends_on)})" if t.depends_on else ""
        lines.append(f"- **{t.task_id} · `{t.assigned_to}`** (`{t.worktree_branch}`){deps_str}: {t.title} — {files_str}")
    lines.extend(
        [
            "",
            "## Verification Plan",
            "- Per-worker `PytestGateMiddleware` + `RubricMiddleware` inside isolated Git worktrees.",
            "- Independent `EvaluatorRubric` 100-point merge gate + post-merge integration `pytest`.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_team_walkthrough_markdown(
    goal: str,
    tasks: list[TeamTask],
    handoffs: dict[str, dict[str, Any]],
    integration_passed: bool,
    integration_output: str,
) -> str:
    lines = [
        "# Team Walkthrough",
        "",
        f"## Goal\n{goal}",
        "",
        "## Changes Made Across Worktrees",
    ]
    for t in tasks:
        ho = handoffs.get(t.assigned_to, {})
        touched = ", ".join(f"`{f}`" for f in ho.get("files_touched", [])) or ", ".join(
            f"`{f}`" for f in t.target_files
        )
        lines.append(
            f"- **{t.task_id} (`{t.assigned_to}` -> `{t.status}`, score={t.review_score}/100)**: "
            f"{t.title} — files: {touched}"
        )
        wt_doc = str((ho.get("artifacts") or {}).get("walkthrough", "")).strip()
        if wt_doc:
            first_para = next(
                (
                    ln.strip()
                    for ln in wt_doc.splitlines()
                    if ln.strip() and not ln.strip().startswith(("#", "<!--"))
                ),
                "",
            )
            if first_para:
                lines.append(f"  - Worker note: {first_para[:200]}")

    lines.extend(
        [
            "",
            "## Validation Results",
            f"- Integration `pytest` on merged `main`: `{'PASSED' if integration_passed else 'FAILED'}`",
            "```text",
            (integration_output.strip()[-1500:] or "(no output)"),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


class MultiAgentTeamCoordinator:
    """
    멀티 에이전트 병렬 Worktree 오케스트레이터 (`langchain-ai/deepagents` 단일 엔진).
    """

    def __init__(
        self,
        workspace_root: str | Path,
        rotator: LLMProviderRotator,
        bare_remote_path: str | Path | None = None,
        settings: AppSettings | None = None,
        model_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.bare_remote_path = Path(bare_remote_path).resolve() if bare_remote_path else None
        self.settings = settings
        self.abyss_dir = self.workspace_root / ".abyss"
        self.mailbox_dir = self.abyss_dir / "mailbox"
        self.tasks_file = self.abyss_dir / "tasks.json"
        self.rotator = rotator
        self.model_factory: Callable[[str, str], Any] = model_factory or self.rotator.build_chat_model
        self.rubric = EvaluatorRubric()
        self.worktree_mgr = GitWorktreeManager(self.workspace_root)

        self.tasks: dict[str, TeamTask] = {}
        self.lead_todos: list[dict[str, str]] = []
        self.team_artifacts: dict[str, str] = {
            "implementation_plan": "",
            "walkthrough": "",
        }
        self.mailbox_log: list[MailboxMessage] = []
        self.event_stream: list[dict[str, Any]] = []
        self.handoffs: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.merge_results: list[dict[str, Any]] = []
        self.last_push_result: dict[str, Any] | None = None
        self.is_running: bool = False
        self.last_status: str = "idle"
        self._event_lock = threading.Lock()
        self._mailbox_lock = threading.Lock()
        self._listeners: list[
            tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop | None]
        ] = []

        self._ensure_base_package()
        if self.bare_remote_path is not None:
            self.ensure_internal_bare_remote()

    def _resolve_model(self, agent_id: str, role: str) -> tuple[Any, str, list[Any]]:
        selection = self.model_factory(agent_id, role)
        if hasattr(selection, "model") and hasattr(selection, "label"):
            mw_fn = getattr(selection, "middleware", None)
            extra_mw = list(mw_fn()) if callable(mw_fn) else []
            return selection.model, str(selection.label), extra_mw
        if isinstance(selection, tuple) and len(selection) == 2:
            return selection[0], str(selection[1]), []
        return selection, getattr(selection, "model_name", type(selection).__name__), []

    def ensure_internal_bare_remote(self) -> str:
        """오프라인 데모 및 테스트용 내부 Bare 원격 저장소를 멱등 초기화합니다."""
        target_bare = self.bare_remote_path or (self.workspace_root.parent / "internal_origin.git")
        self.bare_remote_path = Path(target_bare).resolve()
        return self.worktree_mgr.ensure_internal_bare_remote(self.bare_remote_path)

    def _ensure_base_package(self) -> None:
        """데모 워크스페이스에 기본 패키지 스켈레톤(3+ 파일)과 `.worktreeinclude`를 준비합니다."""
        self.abyss_dir.mkdir(parents=True, exist_ok=True)
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)

        wt_include = self.workspace_root / ".worktreeinclude"
        if not wt_include.exists():
            wt_include.write_text(".env\n.env.local\n", encoding="utf-8")

        gitignore = self.workspace_root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                ".abyss/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n.env\n.env.local\n",
                encoding="utf-8",
            )

        pkg_dir = self.workspace_root / "repo_onboard"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        starter_files = {
            pkg_dir / "__init__.py": (
                '"""RepoOnboard: Local & Internal Git Codebase Onboarding & Socratic Tutor."""\n'
            ),
            pkg_dir / "models.py": (
                '"""Shared domain data structures for RepoOnboard."""\n\n'
                "from dataclasses import dataclass, field\n\n\n"
                "@dataclass\n"
                "class ModuleNode:\n"
                "    module_id: str\n"
                "    file_path: str\n"
                "    imports: list[str] = field(default_factory=list)\n"
            ),
            pkg_dir / "formatting.py": (
                '"""Formatting helpers for onboarding summaries."""\n\n\n'
                "def format_module_badge(module_id: str, line_count: int) -> str:\n"
                '    return f"{module_id} ({max(0, line_count)} lines)"\n'
            ),
        }
        for path, content in starter_files.items():
            if not path.exists():
                path.write_text(content, encoding="utf-8")

        tests_dir = self.workspace_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        starter_test = tests_dir / "test_formatting.py"
        if not starter_test.exists():
            starter_test.write_text(
                "from repo_onboard.formatting import format_module_badge\n\n\n"
                "def test_format_module_badge() -> None:\n"
                '    assert format_module_badge("pkg.core", 42) == "pkg.core (42 lines)"\n',
                encoding="utf-8",
            )

        self.worktree_mgr._run_git(
            [
                "add",
                ".gitignore",
                ".worktreeinclude",
                "repo_onboard/__init__.py",
                "repo_onboard/models.py",
                "repo_onboard/formatting.py",
                "tests/test_formatting.py",
            ]
        )
        status = self.worktree_mgr._run_git(["status", "--porcelain"]).stdout.strip()
        if status:
            self.worktree_mgr._run_git(
                ["commit", "-m", "chore: initialize repo_onboard package skeleton"]
            )

    def _reset_generated_targets_if_present(self, target_files: list[str]) -> None:
        protected = {
            "repo_onboard/__init__.py",
            "repo_onboard/models.py",
            "repo_onboard/formatting.py",
            "tests/test_formatting.py",
        }
        existing = [
            f
            for f in target_files
            if f not in protected and (self.workspace_root / f).exists()
        ]
        if not existing:
            return
        self.worktree_mgr._run_git(["rm", "-f", "--ignore-unmatch", "--", *existing])
        status = self.worktree_mgr._run_git(["status", "--porcelain"]).stdout.strip()
        if status:
            self.worktree_mgr._run_git(
                ["commit", "-m", "chore: reset target files for fresh parallel worktree build"]
            )

    def subscribe_events(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._event_lock:
            self._listeners.append((q, loop))
        return q

    def unsubscribe_events(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._event_lock:
            self._listeners = [(lq, lp) for lq, lp in self._listeners if lq is not q]

    def emit_event(self, event: dict[str, Any]) -> None:
        enriched = {"timestamp": time.time(), **event}
        with self._event_lock:
            self.event_stream.append(enriched)
            listeners = list(self._listeners)
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for q, loop in listeners:
            try:
                if loop is not None and not loop.is_closed() and loop is not current_loop:
                    loop.call_soon_threadsafe(q.put_nowait, enriched)
                else:
                    q.put_nowait(enriched)
            except Exception:  # noqa: BLE001
                pass

    def _save_tasks(self) -> None:
        data = [t.to_dict() for t in self.tasks.values()]
        self.tasks_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def send_mailbox_message(self, sender: str, recipient: str, content: str) -> str:
        with self._mailbox_lock:
            msg = MailboxMessage(
                msg_id=f"msg-{len(self.mailbox_log) + 1:03d}",
                timestamp=time.time(),
                sender=sender,
                recipient=recipient,
                content=content,
            )
            self.mailbox_log.append(msg)
            inbox_file = self.mailbox_dir / f"{recipient}.jsonl"
            with inbox_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")

        self.emit_event(
            {
                "agent_id": sender,
                "role": "teammate",
                "event_type": "mailbox_sent",
                "recipient": recipient,
                "content": content,
            }
        )
        return f"Message delivered to {recipient} ({msg.msg_id})."

    def read_mailbox_messages(self, agent_id: str) -> str:
        with self._mailbox_lock:
            inbox_file = self.mailbox_dir / f"{agent_id}.jsonl"
            if not inbox_file.exists():
                return "No messages in inbox."
            lines = inbox_file.read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(line) for line in lines if line.strip()]
        return "\n".join(f"[{m['sender']} -> {m['recipient']}]: {m['content']}" for m in parsed)

    def update_task_status(self, task_id: str, status: str, note: str = "") -> str:
        if task_id in self.tasks:
            self.tasks[task_id].status = status
            self._save_tasks()
            self.emit_event(
                {
                    "agent_id": self.tasks[task_id].assigned_to,
                    "event_type": "task_updated",
                    "task_id": task_id,
                    "status": status,
                    "note": note,
                }
            )
        return f"Task {task_id} status updated to '{status}'."

    def _build_mailbox_tools(self, worker_id: str) -> list[BaseTool]:
        """LangChain tools exposing the Agent Teams Mailbox & Shared Task Board to a worker."""
        return [
            StructuredTool.from_function(
                name="send_team_message",
                description="Send an asynchronous contract/coordination message to a teammate or lead via Agent Teams Mailbox.",
                func=lambda recipient, message: self.send_mailbox_message(
                    worker_id, recipient, message
                ),
                args_schema=_SendMailboxInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="read_team_messages",
                description="Read incoming messages in your Agent Teams Mailbox.",
                func=lambda: self.read_mailbox_messages(worker_id),
                args_schema=_EmptyToolInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="update_task_status",
                description="Update your task status on the Shared Task Board (in_progress, in_review).",
                func=self.update_task_status,
                args_schema=_UpdateTaskStatusInput,
                infer_schema=False,
            ),
        ]

    def _run_lead_planner_sync(self, user_goal: str) -> tuple[list[TeamTask], str]:
        lead_model, lead_label, lead_mw = self._resolve_model("lead", "lead")
        ro_backend = ReadOnlyGuardedBackend(
            worktree_root=self.workspace_root,
            main_checkout_root=self.workspace_root,
            secrets=collect_env_secrets(),
        )
        lead_runtime = DeepAgentRuntime(
            model=lead_model,
            backend=ro_backend,
            mode="direct",
            workflow="fast",
            completion_cmd=None,
            rubric_enabled=False,
            subagents=False,
            response_format=TeamPlan,
            extra_middleware=lead_mw,
            agent_id="lead",
            provider_label=lead_label,
            event_callback=self.emit_event,
        )
        system_prompt = (
            "You are the Lead Architect of an autonomous coding team. "
            "Inspect the repository if needed, track decomposition steps with `write_todos`, "
            "and return a `TeamPlan` splitting the user's goal into 2–3 isolated Git worktree tasks "
            "with non-overlapping `target_files` (each including an implementation `.py` file and a "
            "`tests/test_*.py` file) and explicit `required_symbols`."
        )
        lead_res = lead_runtime.run(system_prompt, f"Decompose team goal: {user_goal}")
        self.lead_todos = list(lead_res.todos)

        raw_plan = lead_res.structured_response
        if isinstance(raw_plan, dict):
            plan = TeamPlan.model_validate(raw_plan)
        elif isinstance(raw_plan, TeamPlan):
            plan = raw_plan
        else:
            msg = "Lead DeepAgent did not produce a valid structured `TeamPlan`."
            raise RuntimeError(msg)

        tasks: list[TeamTask] = []
        used_workers: set[str] = set()
        for idx, spec in enumerate(plan.tasks, start=1):
            tid = spec.task_id.strip() or f"TASK-{idx}"
            raw_worker = re.sub(r"[^a-z0-9_-]", "", spec.assigned_to.strip().lower())
            if not raw_worker or raw_worker in used_workers:
                raw_worker = (
                    _DEFAULT_WORKER_ROSTER[idx - 1]
                    if idx - 1 < len(_DEFAULT_WORKER_ROSTER)
                    else f"worker-{idx}"
                )
            used_workers.add(raw_worker)
            tasks.append(
                TeamTask(
                    task_id=tid,
                    title=spec.title.strip(),
                    description=spec.description.strip(),
                    assigned_to=raw_worker,
                    worktree_branch=f"worktree-{raw_worker}",
                    target_files=list(spec.target_files),
                    required_symbols=list(spec.required_symbols),
                    depends_on=list(spec.depends_on),
                )
            )

        plan_md = _render_team_plan_markdown(user_goal, plan.summary, tasks)
        self.team_artifacts["implementation_plan"] = plan_md
        team_plan_path = self.abyss_dir / "team_implementation_plan.md"
        team_plan_path.write_text(plan_md, encoding="utf-8")

        self.emit_event(
            {
                "agent_id": "lead",
                "role": "lead",
                "event_type": "lead_planned",
                "provider": lead_label,
                "goal": user_goal,
                "tasks_count": len(tasks),
                "todos": list(self.lead_todos),
                "message": plan.summary,
            }
        )
        return tasks, plan.summary

    def _run_single_worker_sync(self, task: TeamTask, peers: list[str]) -> DeepAgentResult:
        worker_id = task.assigned_to
        wt_info = self.worktree_mgr.worktrees[worker_id]
        task.status = "in_progress"
        self._save_tasks()

        self.emit_event(
            {
                "agent_id": worker_id,
                "role": "worker",
                "branch": wt_info.branch_name,
                "event_type": "worker_started",
                "task_id": task.task_id,
                "worktree_path": str(wt_info.worktree_path),
            }
        )

        worker_model, worker_label, worker_mw = self._resolve_model(worker_id, worker_id)
        artifacts_dir = (
            (self.settings.task_runs_dir if self.settings else self.abyss_dir / "runs")
            / f"team-{worker_id}"
            / "artifacts"
        ).resolve()
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        backend = GuardedWorktreeBackend(
            worktree_root=wt_info.worktree_path,
            main_checkout_root=self.workspace_root,
            secrets=collect_env_secrets(),
            on_block=lambda attempt: self.emit_event(
                {
                    "agent_id": worker_id,
                    "role": "worker",
                    "branch": wt_info.branch_name,
                    "event_type": "isolation_blocked",
                    "operation": attempt.operation,
                    "target": attempt.target,
                    "check_name": attempt.check_name,
                    "reason": attempt.reason,
                }
            ),
        )

        test_targets = [f for f in task.target_files if f.startswith("tests/") and f.endswith(".py")]
        completion_cmd = (
            f"pytest -q -p no:cacheprovider {' '.join(test_targets)}"
            if test_targets
            else "pytest -q -p no:cacheprovider"
        )
        rubric = (
            f"1) Implement `{task.title}` in {', '.join(task.target_files) or 'the worktree'} "
            f"including symbols {', '.join(task.required_symbols) or '(as specified)'}. "
            "2) Share interface/contract updates with teammates via `send_team_message`. "
            "3) Pass unit tests cleanly."
        )

        runtime = DeepAgentRuntime(
            model=worker_model,
            backend=backend,
            mode="worker",
            workflow="planning",
            plan_review="always_proceed",
            artifacts_dir=artifacts_dir,
            completion_cmd=completion_cmd,
            rubric=rubric,
            extra_tools=self._build_mailbox_tools(worker_id),
            extra_middleware=worker_mw,
            agent_id=worker_id,
            provider_label=worker_label,
            event_callback=self.emit_event,
        )

        system_prompt = (
            f"You are `{worker_id}` working inside isolated Git worktree branch `{wt_info.branch_name}`.\n"
            f"Teammates on the board: {', '.join(peers) or 'lead'}.\n"
            "Write `/artifacts/implementation_plan.md` first, implement your assigned files & unit tests, "
            "exchange schema contracts via `send_team_message` / `read_team_messages`, and finish with "
            "`/artifacts/walkthrough.md`."
        )
        task_prompt = (
            f"Task ID: {task.task_id}\n"
            f"Title: {task.title}\n"
            f"Target files: {', '.join(task.target_files)}\n"
            f"Required symbols: {', '.join(task.required_symbols)}\n\n"
            f"Specification:\n{task.description}"
        )

        res = runtime.run(system_prompt, task_prompt, thread_id=f"team-{worker_id}")
        commit_hash = ""
        if res.passed:
            _clean_ephemeral_bytecode(wt_info.worktree_path)
            wt_body = res.artifacts.get("walkthrough", "").strip()
            commit_msg = f"feat({worker_id}): {task.title[:60]}"
            if wt_body:
                clean_lines = [
                    ln
                    for ln in wt_body.splitlines()
                    if not ln.strip().startswith("<!-- abyss:verified-gate:")
                ]
                commit_msg = f"{commit_msg}\n\n" + "\n".join(clean_lines).strip()[:1500]
            commit_hash = self.worktree_mgr.commit_worktree_changes(worker_id, commit_msg)

        task.commit_hash = commit_hash
        task.provider_used = worker_label
        task.status = "in_review" if res.passed else "rejected"
        self._save_tasks()
        self.handoffs[worker_id] = {
            **res.to_dict(),
            "agent_id": worker_id,
            "worktree_branch": wt_info.branch_name,
        }
        return res

    async def execute_full_workflow(
        self, user_goal: str = "Build RepoOnboard: Local & Internal Git Codebase Onboarding Service"
    ) -> dict[str, Any]:
        """Lead 분해 → 격리 Worktree 병렬 Worker 실행 → 독립 루브릭 리뷰 → 순차 머지 & 팀 Walkthrough."""
        self.is_running = True
        self.last_status = "running"
        self.event_stream.clear()
        self.mailbox_log.clear()
        self.handoffs.clear()
        self.reviews.clear()
        self.merge_results.clear()
        self.lead_todos = []
        self.team_artifacts = {"implementation_plan": "", "walkthrough": ""}

        try:
            # Stage 1: Lead `DeepAgentRuntime` decomposes user_goal into 2–3 `TeamTask`s
            # (Fail-Fast: raises `LLMConfigurationError` -> HTTP 503 if unconfigured!)
            task_defs, _lead_summary = await asyncio.to_thread(
                self._run_lead_planner_sync, user_goal
            )
            self.tasks = {t.task_id: t for t in task_defs}
            self._save_tasks()

            self._reset_generated_targets_if_present(
                [fpath for t in task_defs for fpath in t.target_files]
            )

            for t in task_defs:
                wt = self.worktree_mgr.create_worktree(t.assigned_to)
                t.worktree_branch = wt.branch_name
                self.emit_event(
                    {
                        "agent_id": "lead",
                        "role": "lead",
                        "event_type": "worktree_created",
                        "worker_id": t.assigned_to,
                        "branch": wt.branch_name,
                        "worktree_path": str(wt.worktree_path),
                        "locked": wt.locked,
                    }
                )
            self._save_tasks()

            all_worker_ids = [t.assigned_to for t in task_defs]
            wave_1 = [t for t in task_defs if not t.depends_on]
            wave_2 = [t for t in task_defs if t.depends_on]
            if not wave_1:
                wave_1, wave_2 = task_defs, []

            # Stage 2A: Parallel execution of independent workers via `asyncio.gather`
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        self._run_single_worker_sync,
                        t,
                        [w for w in all_worker_ids if w != t.assigned_to] + ["lead"],
                    )
                    for t in wave_1
                )
            )

            # Stage 2B: Sync upstream branches into dependent worker worktrees and run in parallel
            if wave_2:
                for dep_task in wave_2:
                    dep_wt = self.worktree_mgr.worktrees[dep_task.assigned_to]
                    for upstream_id in dep_task.depends_on:
                        upstream_task = self.tasks.get(upstream_id)
                        if upstream_task and upstream_task.commit_hash:
                            self.worktree_mgr._run_git(
                                [
                                    "merge",
                                    "--no-ff",
                                    upstream_task.worktree_branch,
                                    "-m",
                                    f"chore: sync upstream {upstream_task.worktree_branch}",
                                ],
                                cwd=dep_wt.worktree_path,
                            )
                await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            self._run_single_worker_sync,
                            t,
                            [w for w in all_worker_ids if w != t.assigned_to] + ["lead"],
                        )
                        for t in wave_2
                    )
                )

            # Stage 3: Independent Reviewer Gate (`EvaluatorRubric`)
            for t in task_defs:
                worker_id = t.assigned_to
                wt_info = self.worktree_mgr.worktrees[worker_id]
                diff_text = self.worktree_mgr.get_worktree_diff(worker_id)

                test_targets = [
                    f for f in t.target_files if f.startswith("tests/") and f.endswith(".py")
                ] or ["tests"]
                env = _safe_env(wt_info.worktree_path)
                pytest_proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *test_targets, "-v"],
                    cwd=str(wt_info.worktree_path),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                pytest_out = (pytest_proc.stdout or "") + (pytest_proc.stderr or "")
                sent_count = sum(1 for m in self.mailbox_log if m.sender == worker_id)

                decision: ReviewDecision = self.rubric.evaluate_worktree_submission(
                    worker_id=worker_id,
                    branch_name=wt_info.branch_name,
                    git_diff=diff_text,
                    pytest_exit_code=pytest_proc.returncode,
                    pytest_output=pytest_out,
                    required_symbols=t.required_symbols,
                    mailbox_messages_sent=sent_count,
                )

                t.review_score = decision.total_score
                t.status = "approved" if decision.approved else "rejected"
                self._save_tasks()
                self.reviews[worker_id] = {
                    **decision.to_dict(),
                    "diff": diff_text,
                    "pytest_output": pytest_out.strip(),
                }

                self.send_mailbox_message("reviewer", worker_id, decision.feedback_for_worker)
                self.emit_event(
                    {
                        "agent_id": "reviewer",
                        "role": "reviewer",
                        "event_type": "review_completed",
                        "worker_id": worker_id,
                        "branch": wt_info.branch_name,
                        "approved": decision.approved,
                        "score": decision.total_score,
                    }
                )

            # Stage 4: Sequential Git Merge onto `main` + Team Walkthrough Synthesis
            for t in task_defs:
                worker_id = t.assigned_to
                if t.status != "approved":
                    continue
                merge_res = self.worktree_mgr.merge_worktree_branch(worker_id)
                self.merge_results.append(merge_res)
                if merge_res["merged"]:
                    t.status = "merged"
                    self._save_tasks()
                    self.emit_event(
                        {
                            "agent_id": "lead",
                            "role": "lead",
                            "event_type": "branch_merged",
                            "worker_id": worker_id,
                            "branch": merge_res["branch"],
                            "main_head": merge_res["main_head"],
                        }
                    )

            main_env = _safe_env(self.workspace_root)
            final_pytest = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests", "-q"],
                cwd=str(self.workspace_root),
                env=main_env,
                capture_output=True,
                text=True,
                check=False,
            )
            integration_out = ((final_pytest.stdout or "") + (final_pytest.stderr or "")).strip()

            team_wt = _render_team_walkthrough_markdown(
                user_goal,
                task_defs,
                self.handoffs,
                final_pytest.returncode == 0,
                integration_out,
            )
            self.team_artifacts["walkthrough"] = team_wt
            (self.abyss_dir / "team_walkthrough.md").write_text(team_wt, encoding="utf-8")

            remote_info = self.worktree_mgr.get_remote_info()
            if remote_info["configured"] and self.merge_results:
                self.last_push_result = await asyncio.to_thread(
                    self.worktree_mgr.push_branch,
                    self.worktree_mgr.get_default_branch(),
                    settings=self.settings,
                )

            self.last_status = "completed"
            self.is_running = False
            self.emit_event(
                {
                    "agent_id": "lead",
                    "role": "lead",
                    "event_type": "workflow_finished",
                    "all_merged": all(t.status == "merged" for t in self.tasks.values()),
                    "integration_pytest_output": integration_out,
                }
            )
            return self.get_dashboard_state()
        except Exception:
            self.last_status = "failed"
            raise
        finally:
            self.is_running = False

    def get_dashboard_state(self) -> dict[str, Any]:
        status = (
            "running"
            if self.is_running
            else (
                "completed"
                if (self.last_status == "completed" or self.merge_results)
                else self.last_status
            )
        )
        merged_branches = [m["branch"] for m in self.merge_results if m.get("merged")]
        remote_info = self.worktree_mgr.get_remote_info()
        if self.last_push_result is not None:
            remote_info = {**remote_info, "last_push": self.last_push_result}

        return {
            "engine": "langchain-ai/deepagents",
            "status": status,
            "is_running": self.is_running,
            "workspace_root": str(self.workspace_root),
            "lead_todos": list(self.lead_todos),
            "team_artifacts": dict(self.team_artifacts),
            "tasks": [t.to_dict() for t in self.tasks.values()],
            "worktrees": {
                wid: info.to_dict() for wid, info in self.worktree_mgr.worktrees.items()
            },
            "mailbox": [m.to_dict() for m in self.mailbox_log],
            "handoffs": self.handoffs,
            "reviews": self.reviews,
            "merges": self.merge_results,
            "merged_branches": merged_branches,
            "internal_remote": remote_info,
            "git_graph": self.worktree_mgr.get_git_graph_log(limit=18),
            "rotator": self.rotator.get_state(),
            "recent_events": self.event_stream[-60:],
        }
