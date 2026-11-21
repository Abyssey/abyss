"""
tests/test_coding_agent_tasks.py
Phase 5 End-to-End Verification Suite for `CodingTaskService`:
  - HTTP 503 Fail-Fast (`⛔ LLM CONFIG REQUIRED`) when no LLM credentials exist
  - Antigravity `request_review` (`awaiting_plan_review` -> `POST /plan/approve` + `GET /tasks/{id}`)
  - `walkthrough.md` -> Git commit message & MR description generation
  - Gate-protected Git hygiene (failed gate never commits or pushes)
  - Live HTML Preview (`CSP: sandbox allow-scripts`, `X-Content-Type-Options: nosniff`, XSS escaping)
  - Act 4 Read-Only Detached-Worktree Review + `ReviewFindings` + `EvaluatorRubric.merge_llm_findings`
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import AppSettings
from app.core.llm import LLM_CONFIG_REQUIRED_BANNER
from app.domains.workflow.evaluator import EvaluatorRubric
from app.main import create_app
from tests.helpers_llm_doubles import (
    ScriptedDeepAgentChatModel,
    ai_text,
    ai_tool_call,
    grader_verdict,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics (run under WSL/Linux)")

PLAN_MD = """\
# Implementation Plan

## Goal
Scan real workspace Python modules via `ast` and render a standalone HTML5 map.

## User Review Required
None.

## Proposed Changes
- [NEW] `project_map/builder.py`: AST scanner + HTML generator with `html.escape`.
- [NEW] `tests/test_project_map.py`: unit test verifying real scanned files & HTML safety.

## Verification Plan
- Run `pytest -q -p no:cacheprovider`.
"""

WALKTHROUGH_MD = """\
# Walkthrough

## Changes Made
- Created `project_map/builder.py` scanning workspace `.py` files with `ast` and rendering `project_map/index.html`.
- Added `tests/test_project_map.py` verifying zero external CDNs and HTML escaping.

## Validation Results
- Verified `pytest` suite passes cleanly inside the isolated worktree.
"""

MINI_AST_BUILDER_PY = '''\
"""Minimal real AST scanner & HTML5 map written by scripted builder test double."""
from __future__ import annotations

import ast
import html
import os
from pathlib import Path


def build_map(root: Path, out_html: Path, title: str = "Workspace") -> int:
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in {".git", ".abyss", "__pycache__", ".pytest_cache", ".venv"}
        )
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            symbols: list[str] = []
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
                symbols = [
                    n.name for n in ast.iter_child_nodes(tree)
                    if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                ]
            except SyntaxError:
                pass
            sym_text = ", ".join(html.escape(s) for s in symbols)
            entries.append(f"<li><code>{html.escape(rel)}</code> — {sym_text}</li>")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    body = "\\n".join(entries)
    out_html.write_text(
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — Interactive Project Map</title></head>"
        f"<body><h1>{html.escape(title)}</h1><ul>{body}</ul></body></html>",
        encoding="utf-8",
    )
    return len(entries)


if __name__ == "__main__":
    build_map(Path("."), Path("project_map/index.html"), Path.cwd().name)
'''

MINI_AST_TEST_PY = '''\
from pathlib import Path
from project_map.builder import build_map


def test_project_map_scans_real_files(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out = tmp_path / "index.html"
    count = build_map(repo_root, out, title="TestMap")
    assert count >= 1
    text = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in text
'''


def _make_isolated_test_app(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    demo_ws = tmp_path / "demo_workspace" / "repo_onboard"
    bare_remote = tmp_path / "demo_workspace" / "internal_origin.git"
    settings = AppSettings(
        repo_root=repo_root,
        static_dir=repo_root / "app" / "static",
        demo_workspace=demo_ws,
        code_mode_sandbox=demo_ws / ".abyss" / "code_mode_sandbox",
        internal_bare_remote=bare_remote,
        task_runs_dir=tmp_path / "demo_workspace" / "task_runs",
    )
    application = create_app(settings=settings)
    for pid in ("qwen", "gemini", "openai"):
        cfg = application.state.container.rotator.providers[pid]
        cfg.enabled = False
        cfg.api_key = ""
    return application, settings


def _make_builder_double(*, repo_title: str = "repo_onboard") -> ScriptedDeepAgentChatModel:
    builder_code = MINI_AST_BUILDER_PY.replace("Path.cwd().name", repr(repo_title))
    return ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/implementation_plan.md", "content": PLAN_MD},
            ),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Write AST scanner & HTML generator", "status": "in_progress"},
                        {"content": "Generate index.html & run pytest", "status": "pending"},
                    ]
                },
            ),
            ai_tool_call("write_file", {"file_path": "/project_map/__init__.py", "content": ""}),
            ai_tool_call(
                "write_file",
                {"file_path": "/project_map/builder.py", "content": builder_code},
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/tests/test_project_map.py", "content": MINI_AST_TEST_PY},
            ),
            ai_tool_call("execute", {"command": "python -m project_map.builder"}),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Write AST scanner & HTML generator", "status": "completed"},
                        {"content": "Generate index.html & run pytest", "status": "completed"},
                    ]
                },
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/walkthrough.md", "content": WALKTHROUGH_MD},
            ),
            ai_text("Built `project_map/index.html` and verified tests."),
        ],
        grader_steps=[grader_verdict("satisfied", "AST scan, standalone HTML, and pytest verified.")],
    )


def _make_reviewer_double(
    findings: list[dict] | None = None,
    summary: str = "독립 LLM 리뷰 완료.",
) -> ScriptedDeepAgentChatModel:
    return ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "ReviewFindings",
                {
                    "summary": summary,
                    "findings": findings or [],
                },
            )
        ]
    )


def test_presets_endpoint_exposes_5_canonical_korean_prompts(tmp_path: Path) -> None:
    from app.domains.workflow.schemas import BuildTaskRequest

    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        res = client.get("/api/workflow/tasks/presets")
        assert res.status_code == 200
        data = res.json()
        prompts = data["storyline_prompts"]
        assert len(prompts) == 5
        assert [p["prompt"] for p in prompts] == [
            "이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘",
            "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
            "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘",
            "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘",
            "방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘",
        ]
        assert [bp["id"] for bp in data["build_presets"]] == [
            "project_map_app",
            "inspection_dashboard",
            "custom",
        ]
        assert BuildTaskRequest().instruction == "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"


def test_build_and_review_fail_fast_with_http_503_when_no_llm_credentials(tmp_path: Path) -> None:
    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        b_res = client.post(
            "/api/workflow/tasks/build",
            json={"repo_url_or_path": "demo", "preset": "project_map_app"},
        )
        assert b_res.status_code == 503
        assert b_res.json()["error"] == LLM_CONFIG_REQUIRED_BANNER

        r_res = client.post(
            "/api/workflow/tasks/review",
            json={"preset": "defective_mr"},
        )
        assert r_res.status_code == 503
        assert r_res.json()["error"] == LLM_CONFIG_REQUIRED_BANNER


@POSIX_ONLY
def test_act2_request_review_plan_approve_live_preview_and_act4_clean_review(
    tmp_path: Path,
) -> None:
    test_app, settings = _make_isolated_test_app(tmp_path)
    builder_double = _make_builder_double(repo_title="repo_onboard")
    reviewer_double = _make_reviewer_double(findings=[], summary="깨끗한 구현입니다.")

    def _factory(_aid: str, role: str):
        if role == "reviewer":
            return reviewer_double, "scripted:reviewer"
        return builder_double, "scripted:builder"

    test_app.state.container.task_service.model_factory = _factory

    with TestClient(test_app) as client:
        # 1) Default `request_review` pauses at `awaiting_plan_review` before touching worktree code
        paused_res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": "demo",
                "instruction": "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
                "preset": "project_map_app",
                "push_to_remote": True,
                "plan_review": "request_review",
            },
        )
        assert paused_res.status_code == 200, paused_res.text
        paused = paused_res.json()
        task_id = paused["task_id"]
        assert paused["status"] == "awaiting_plan_review"
        assert paused["commit_hash"] == ""
        assert paused["pushed_to_remote"] is False
        assert "# Implementation Plan" in paused["artifacts"]["implementation_plan"]

        # Task detail API returns persisted artifacts while awaiting review
        detail_res = client.get(f"/api/workflow/tasks/{task_id}")
        assert detail_res.status_code == 200
        assert detail_res.json()["status"] == "awaiting_plan_review"
        assert "## Proposed Changes" in detail_res.json()["artifacts"]["implementation_plan"]

        # 2) User clicks Proceed (`/plan/approve`) with an inline comment -> executes & commits!
        approve_res = client.post(
            f"/api/workflow/tasks/{task_id}/plan/approve",
            json={"proceed": True, "comments": "Ensure HTML escaping on all symbol names."},
        )
        assert approve_res.status_code == 200, approve_res.text
        b = approve_res.json()

        assert b["status"] == "completed"
        assert b["test_result"]["passed"] is True
        assert b["pushed_to_remote"] is True
        assert "project_map/builder.py" in b["changed_files"]
        assert "project_map/index.html" in b["changed_files"]
        assert "tests/test_project_map.py" in b["changed_files"]
        assert "## Changes Made" in b["mr_description"]
        assert "- [x] Generate index.html & run pytest" in b["artifacts"]["task"]
        assert "Exit Code: `0`" in b["artifacts"]["walkthrough"]

        # Commit message body on the branch includes the walkthrough summary
        log_body = subprocess.run(
            ["git", "log", "-1", "--format=%B", b["branch_name"]],
            cwd=str(settings.demo_workspace),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "Changes Made" in log_body

        # Bare remote received pushed branch and `main` working tree is untouched
        ls_remote = subprocess.run(
            ["git", "ls-remote", "--heads", str(settings.internal_bare_remote)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert f"refs/heads/{b['branch_name']}" in ls_remote
        assert not (settings.demo_workspace / "project_map").exists()

        # 3) Live Preview serves real AST-scanned HTML with strict CSP & nosniff headers
        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        assert "sandbox allow-scripts" in prev.headers.get("content-security-policy", "")
        assert prev.headers.get("x-content-type-options") == "nosniff"
        assert "repo_onboard/formatting.py" in prev.text
        assert "format_module_badge" in prev.text
        assert client.get("/api/workflow/tasks/nonexistent-task/preview").status_code == 404
        assert client.get("/api/workflow/tasks/..%2F..%2Fetc/preview").status_code == 404

        # 4) Act 4-B Independent Read-Only Review in detached worktree APPROVES without mutating SHA
        head_before = subprocess.run(
            ["git", "rev-parse", b["branch_name"]],
            cwd=str(settings.demo_workspace),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        rev_res = client.post(
            "/api/workflow/tasks/review",
            json={
                "repo_url_or_path": "demo",
                "branch_name": b["branch_name"],
                "base_branch": "main",
                "run_tests": True,
            },
        )
        assert rev_res.status_code == 200, rev_res.text
        rev = rev_res.json()
        assert rev["decision"] == "APPROVE"
        assert rev["score"] >= 80
        assert rev["read_only_verified"] is True
        assert rev["provider_used"] == "scripted:reviewer"

        head_after = subprocess.run(
            ["git", "rev-parse", b["branch_name"]],
            cwd=str(settings.demo_workspace),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_before == head_after


@POSIX_ONLY
def test_failed_build_gate_records_failed_and_never_commits_or_pushes(tmp_path: Path) -> None:
    test_app, settings = _make_isolated_test_app(tmp_path)
    failing_builder = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/implementation_plan.md", "content": PLAN_MD},
            ),
            ai_tool_call(
                "write_file",
                {
                    "file_path": "/tests/test_broken.py",
                    "content": "def test_broken():\n    assert 1 == 2\n",
                },
            ),
            ai_text("Done 1"),
            ai_text("Done 2"),
            ai_text("Done 3"),
            ai_text("Done 4"),
        ]
    )
    test_app.state.container.task_service.model_factory = (
        lambda _aid, _role: (failing_builder, "scripted:failing")
    )

    with TestClient(test_app) as client:
        res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": "demo",
                "preset": "project_map_app",
                "push_to_remote": True,
                "plan_review": "always_proceed",
            },
        )
        assert res.status_code == 200, res.text
        b = res.json()
        assert b["status"] == "failed"
        assert b["commit_hash"] == ""
        assert b["pushed_to_remote"] is False
        assert b["preview_url"] is None

        ls_remote = subprocess.run(
            ["git", "ls-remote", "--heads", str(settings.internal_bare_remote)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert b["branch_name"] not in ls_remote


@POSIX_ONLY
def test_code_mode_build_self_isolation_secret_scrub_and_xss_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERNAL_GITLAB_TOKEN", "CANARY_POD_GITLAB_77777")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "CANARY_POD_AWS_88888")

    xss_folder = tmp_path / "xss_project"
    xss_folder.mkdir()
    (xss_folder / "<img src=x onerror=alert(1)>.py").write_text("X = 1\n", encoding="utf-8")

    repl_snippet = (
        f"await write('project_map/__init__.py', '')\n"
        f"await write('project_map/builder.py', {MINI_AST_BUILDER_PY!r})\n"
        f"await write('tests/test_project_map.py', {MINI_AST_TEST_PY!r})\n"
        "out = await sh('python -m project_map.builder')\n"
        "{'exit_code': out.exit_code}\n"
    )
    cm_builder = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/implementation_plan.md", "content": PLAN_MD},
            ),
            ai_tool_call("run_python", {"code": repl_snippet}),
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/walkthrough.md", "content": WALKTHROUGH_MD},
            ),
            ai_text("Built via Code Mode REPL."),
        ],
        grader_steps=[grader_verdict("satisfied")],
    )

    test_app, _ = _make_isolated_test_app(tmp_path)
    test_app.state.container.task_service.model_factory = (
        lambda _aid, _role: (cm_builder, "scripted:cm-builder")
    )

    with TestClient(test_app) as client:
        res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": str(xss_folder),
                "instruction": "",
                "preset": "project_map_app",
                "use_code_mode": True,
                "push_to_remote": False,
                "plan_review": "always_proceed",
            },
        )
        assert res.status_code == 200, res.text
        b = res.json()
        assert b["status"] == "completed"
        assert any(c["tool_name"].startswith("REPL:") for c in b["guardrail_checks"])
        assert all(not f.endswith(".pyc") and "__pycache__" not in f for f in b["changed_files"])
        assert "CANARY_POD_GITLAB_77777" not in b["test_result"]["output"]
        assert "CANARY_POD_AWS_88888" not in b["test_result"]["output"]
        assert not (xss_folder / "project_map").exists()

        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        assert "<img src=x onerror=alert(1)>" not in prev.text
        assert "&lt;img src=x onerror=alert(1)&gt;.py" in prev.text


def test_act4_defective_mr_and_merge_llm_findings_dedup_and_recompute(tmp_path: Path) -> None:
    test_app, _ = _make_isolated_test_app(tmp_path)

    # 1) Direct unit check on `EvaluatorRubric.merge_llm_findings` (pure stdlib)
    rubric = EvaluatorRubric()
    clean_diff = """diff --git a/service/calc.py b/service/calc.py
new file mode 100644
--- /dev/null
+++ b/service/calc.py
@@ -0,0 +1,4 @@
+def divide(a: float, b: float) -> float:
+    return a / b
diff --git a/tests/test_calc.py b/tests/test_calc.py
new file mode 100644
--- /dev/null
+++ b/tests/test_calc.py
@@ -0,0 +1,3 @@
+from service.calc import divide
+def test_divide():
+    assert divide(4, 2) == 2
"""
    base = rubric.analyze_diff_for_review(clean_diff, pytest_exit_code=0)
    assert base.decision == "APPROVE"

    # High-severity logic finding from LLM flips APPROVE -> REQUEST_CHANGES and deduplicates repeats
    merged = rubric.merge_llm_findings(
        base,
        [
            {
                "severity": "high",
                "file": "service/calc.py",
                "line": 2,
                "comment": "`b == 0`일 때 `ZeroDivisionError`가 발생해 결제 정산이 중단될 수 있습니다.",
                "suggestion": "`if b == 0: raise ValueError(...)` 검증을 추가하세요.",
            },
            {
                "severity": "high",
                "file": "/service/calc.py",
                "line": 2,
                "comment": "Duplicate on same file:line:severity must be deduplicated.",
                "suggestion": "Dup",
            },
        ],
        llm_summary="0 나눗셈 예외 미처리 결함이 발견되었습니다.",
    )
    assert merged.decision == "REQUEST_CHANGES"
    assert len(merged.findings) == 1
    assert merged.score <= 80
    assert "0 나눗셈" in merged.summary

    # 2) HTTP endpoint check on `defective_mr` with LLM reviewer double
    test_app.state.container.task_service.model_factory = lambda _aid, _role: (
        _make_reviewer_double(
            findings=[
                {
                    "severity": "medium",
                    "file": "inspection_dashboard/api_handler.py",
                    "line": 8,
                    "comment": "`parsed` 변수가 파싱 후 사용되지 않고 버려집니다.",
                    "suggestion": "파싱된 페이로드를 검증하거나 반환값에 반영하세요.",
                }
            ]
        ),
        "scripted:reviewer",
    )
    with TestClient(test_app) as client:
        res_preset = client.post("/api/workflow/tasks/review", json={"preset": "defective_mr"})
        assert res_preset.status_code == 200, res_preset.text
        rev = res_preset.json()
        assert rev["decision"] == "REQUEST_CHANGES"
        assert rev["score"] < 80
        assert rev["read_only_verified"] is True
        assert any("비밀키" in f["comment"] or "하드코딩" in f["comment"] for f in rev["findings"])
        assert any("parsed" in f["comment"] for f in rev["findings"])


def test_act4_review_rejects_git_flag_injection_in_commit_range_and_branches(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "git_output_pwned.txt"
    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        bad_payloads = [
            {"repo_url_or_path": "demo", "commit_range": f"--output={marker}", "run_tests": False},
            {"repo_url_or_path": "demo", "commit_range": "--stat", "run_tests": False},
            {"repo_url_or_path": "demo", "branch_name": "--upload-pack=id", "run_tests": False},
            {"repo_url_or_path": "demo", "base_branch": "-c foo=bar", "run_tests": False},
        ]
        for payload in bad_payloads:
            res = client.post("/api/workflow/tasks/review", json=payload)
            assert res.status_code == 400, f"Expected 400 for {payload}, got {res.status_code}"
        assert not marker.exists()
