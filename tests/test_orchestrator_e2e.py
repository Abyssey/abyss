"""
End-to-End Integration Test for `MultiAgentTeamCoordinator` (`app/domains/workflow/coordinator.py`)
running on `langchain-ai/deepagents` (`DeepAgentRuntime`).

Verifies:
1. Fail-Fast HTTP 503 (`⛔ LLM CONFIG REQUIRED`) when `POST /api/workflow/run` is called without live LLM keys
2. Lead `DeepAgentRuntime` (`response_format=TeamPlan`) decomposes `user_goal` into 3 tasks (`.abyss/tasks.json`)
   and writes team `implementation_plan.md` + `lead_todos`
3. 3 isolated Git Worktrees are created and locked (`.abyss/worktrees/worker-*`)
4. Worker 1 (`worker-analyzer`) triggers and records a `GuardedWorktreeBackend` guard block (`git -C` refusal)
   before completing its Antigravity 3-phase workflow (`implementation_plan.md` -> code/tests -> `walkthrough.md`)
5. Wave 1 workers (`worker-analyzer`, `worker-tutor`) run in parallel via `asyncio.gather`, exchange schema
   contracts via Agent Teams Mailbox tools (`send_team_message`, `read_team_messages`), and pass `PytestGateMiddleware` + `RubricMiddleware`
6. Wave 2 dependent worker (`worker-web`, `depends_on=["TASK-1", "TASK-2"]`) receives synced upstream branches,
   builds `RepoOnboardService`, and passes `PytestGateMiddleware` + `RubricMiddleware`
7. Reviewer `EvaluatorRubric` scores all 3 worktree branches (100/100) and approves them
8. Lead merges all 3 branches onto `main`, runs integration `pytest`, synthesizes team `walkthrough.md`,
   and pushes `main` to `internal_origin.git`
9. The merged `RepoOnboardService` dynamically analyzes this live repository and evaluates Socratic Q&A
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import AppSettings
from app.core.llm.provider_rotator import LLM_CONFIG_REQUIRED_BANNER, LLMProviderRotator
from app.domains.workflow.coordinator import MultiAgentTeamCoordinator
from app.main import create_app
from tests.helpers_llm_doubles import (
    ScriptedDeepAgentChatModel,
    ai_text,
    ai_tool_call,
    grader_verdict,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="GuardedWorktreeBackend POSIX shell execution requires Linux/WSL.",
)

ANALYZER_CODE = '''"""AST repository analyzer implemented by worker-analyzer."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from repo_onboard.models import ModuleNode


class CodebaseAnalyzer:
    """Scans Python files in a repository and builds an Archify module graph."""

    def analyze_directory(self, root_path: str | Path) -> dict[str, Any]:
        root = Path(root_path).resolve()
        py_files = sorted(
            p
            for p in root.rglob("*.py")
            if ".git" not in p.parts
            and ".venv" not in p.parts
            and ".abyss" not in p.parts
            and "__pycache__" not in p.parts
        )
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        for path in py_files:
            rel = path.relative_to(root).as_posix()
            mod_id = rel[:-3].replace("/", ".")
            imports: list[str] = []
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
                for stmt in ast.walk(tree):
                    if isinstance(stmt, ast.ImportFrom) and stmt.module:
                        imports.append(stmt.module)
                    elif isinstance(stmt, ast.Import):
                        imports.extend(alias.name for alias in stmt.names)
            except SyntaxError:
                imports = []
            node = ModuleNode(module_id=mod_id, file_path=rel, imports=sorted(set(imports)))
            nodes.append(
                {
                    "id": node.module_id,
                    "file_path": node.file_path,
                    "imports": node.imports,
                }
            )
            for imp in node.imports:
                edges.append({"source": node.module_id, "target": imp})
        return {
            "root": str(root),
            "total_files": len(py_files),
            "archify_graph": {"nodes": nodes, "edges": edges},
        }
'''

ANALYZER_TEST = '''from pathlib import Path

from repo_onboard.analyzer import CodebaseAnalyzer


def test_codebase_analyzer_builds_archify_graph(tmp_path: Path) -> None:
    pkg = tmp_path / "sample_pkg"
    pkg.mkdir()
    (pkg / "alpha.py").write_text("import json\\n\\ndef run() -> int:\\n    return 1\\n", encoding="utf-8")
    (pkg / "beta.py").write_text("from sample_pkg import alpha\\n", encoding="utf-8")

    res = CodebaseAnalyzer().analyze_directory(tmp_path)
    assert res["total_files"] == 2
    ids = {n["id"] for n in res["archify_graph"]["nodes"]}
    assert "sample_pkg.alpha" in ids
    assert "sample_pkg.beta" in ids
'''

TUTOR_CODE = '''"""3-stage Socratic onboarding tutor implemented by worker-tutor."""

from __future__ import annotations

from typing import Any


class SocraticOnboardingTutor:
    """Generates and grades a 3-stage Socratic onboarding question ladder."""

    def build_ladder(self, analysis: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = analysis.get("archify_graph", {}).get("nodes", [])
        lead_file = nodes[0]["file_path"] if nodes else "repo_onboard/models.py"
        total = int(analysis.get("total_files", len(nodes)))
        return [
            {
                "id": "q1-entry",
                "stage": 1,
                "question": f"Which tracked file anchors the scanned repository ({total} Python files)?",
                "choices": [lead_file, "nonexistent/ghost.py", "tmp/scratch.py"],
                "correct_index": 0,
            },
            {
                "id": "q2-isolation",
                "stage": 2,
                "question": "How are parallel coding workers isolated during repository modification?",
                "choices": [
                    "Shared mutable working tree",
                    "Dedicated locked Git worktrees with path & command guardrails",
                    "Direct pushes to main without review",
                ],
                "correct_index": 1,
            },
            {
                "id": "q3-verification",
                "stage": 3,
                "question": "What gates must pass before a worker branch merges onto main?",
                "choices": [
                    "Automated pytest suite + independent EvaluatorRubric threshold",
                    "No verification required",
                    "Manual print debugging only",
                ],
                "correct_index": 0,
            },
        ]

    def evaluate_answer(
        self, ladder: list[dict[str, Any]], question_id: str, selected_index: int
    ) -> dict[str, Any]:
        for item in ladder:
            if item["id"] == question_id:
                ok = int(selected_index) == int(item["correct_index"])
                return {
                    "question_id": question_id,
                    "is_correct": ok,
                    "correct_index": item["correct_index"],
                    "feedback": "Exact match — architectural invariant confirmed."
                    if ok
                    else "Re-check the module graph and worktree isolation boundary.",
                }
        raise KeyError(f"Unknown question_id: {question_id}")
'''

TUTOR_TEST = '''from repo_onboard.onboarding_tutor import SocraticOnboardingTutor


def test_socratic_tutor_ladder_and_grading() -> None:
    tutor = SocraticOnboardingTutor()
    fake_analysis = {
        "total_files": 4,
        "archify_graph": {"nodes": [{"id": "app.main", "file_path": "app/main.py", "imports": []}]},
    }
    ladder = tutor.build_ladder(fake_analysis)
    assert len(ladder) == 3
    verdict = tutor.evaluate_answer(ladder, "q1-entry", selected_index=0)
    assert verdict["is_correct"] is True
'''

SERVICE_CODE = '''"""Unified RepoOnboard facade implemented by worker-web."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from repo_onboard.analyzer import CodebaseAnalyzer
from repo_onboard.formatting import format_module_badge
from repo_onboard.onboarding_tutor import SocraticOnboardingTutor


class RepoOnboardService:
    """Combines AST codebase analysis with 3-stage Socratic onboarding Q&A."""

    def __init__(self) -> None:
        self.analyzer = CodebaseAnalyzer()
        self.tutor = SocraticOnboardingTutor()
        self._last_ladder: list[dict[str, Any]] = []

    def onboard_repository(self, repo_path: str | Path) -> dict[str, Any]:
        analysis = self.analyzer.analyze_directory(repo_path)
        self._last_ladder = self.tutor.build_ladder(analysis)
        badges = [
            format_module_badge(n["id"], len(n.get("imports", [])) + 1)
            for n in analysis["archify_graph"]["nodes"][:5]
        ]
        return {
            "analysis": analysis,
            "badges": badges,
            "socratic_ladder": self._last_ladder,
        }

    def check_socratic_answer(self, question_id: str, selected_index: int) -> dict[str, Any]:
        return self.tutor.evaluate_answer(self._last_ladder, question_id, selected_index)
'''

SERVICE_TEST = '''from pathlib import Path

from repo_onboard.service import RepoOnboardService


def test_repo_onboard_service_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "mod_a.py").write_text("x = 42\\n", encoding="utf-8")
    svc = RepoOnboardService()
    pkg = svc.onboard_repository(tmp_path)
    assert pkg["analysis"]["total_files"] >= 1
    assert len(pkg["socratic_ladder"]) == 3
    q0 = pkg["socratic_ladder"][0]
    res = svc.check_socratic_answer(q0["id"], q0["correct_index"])
    assert res["is_correct"] is True
'''


def _worker_plan(title: str, target_files: list[str]) -> str:
    joined = ", ".join(f"`{f}`" for f in target_files)
    return (
        f"# Implementation Plan — {title}\n\n"
        f"## Proposed Changes\n- Implement {joined} inside isolated Git worktree.\n\n"
        "## Verification Plan\n- Run `pytest` via `PytestGateMiddleware` and share contract via mailbox.\n"
    )


def _worker_walkthrough(title: str, target_files: list[str]) -> str:
    joined = ", ".join(f"`{f}`" for f in target_files)
    return (
        f"# Walkthrough — {title}\n\n"
        f"## Changes Made\n- Created and verified {joined} and synchronized schema contracts.\n\n"
        "## Validation Results\n- Automated unit tests executed inside isolated Git worktree.\n"
    )


def _make_lead_double() -> ScriptedDeepAgentChatModel:
    return ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Decompose RepoOnboard into 3 isolated worktree slices", "status": "completed"},
                        {"content": "Define required symbols and wave dependencies", "status": "completed"},
                    ]
                },
            ),
            ai_tool_call(
                "TeamPlan",
                {
                    "summary": "Split RepoOnboard into parallel AST Analyzer + Socratic Tutor (Wave 1) followed by unified Facade Service (Wave 2).",
                    "tasks": [
                        {
                            "task_id": "TASK-1",
                            "title": "AST Codebase Analyzer & Archify Graph",
                            "description": "Implement CodebaseAnalyzer.analyze_directory in repo_onboard/analyzer.py with unit tests.",
                            "assigned_to": "worker-analyzer",
                            "target_files": ["repo_onboard/analyzer.py", "tests/test_analyzer.py"],
                            "required_symbols": ["CodebaseAnalyzer", "analyze_directory"],
                            "depends_on": [],
                        },
                        {
                            "task_id": "TASK-2",
                            "title": "3-Stage Socratic Onboarding Tutor",
                            "description": "Implement SocraticOnboardingTutor in repo_onboard/onboarding_tutor.py with unit tests.",
                            "assigned_to": "worker-tutor",
                            "target_files": [
                                "repo_onboard/onboarding_tutor.py",
                                "tests/test_onboarding_tutor.py",
                            ],
                            "required_symbols": ["SocraticOnboardingTutor", "build_ladder", "evaluate_answer"],
                            "depends_on": [],
                        },
                        {
                            "task_id": "TASK-3",
                            "title": "Unified RepoOnboard Service Facade",
                            "description": "Combine CodebaseAnalyzer and SocraticOnboardingTutor in repo_onboard/service.py.",
                            "assigned_to": "worker-web",
                            "target_files": ["repo_onboard/service.py", "tests/test_service.py"],
                            "required_symbols": [
                                "RepoOnboardService",
                                "onboard_repository",
                                "check_socratic_answer",
                            ],
                            "depends_on": ["TASK-1", "TASK-2"],
                        },
                    ],
                },
            ),
        ]
    )


def _make_analyzer_worker_double() -> ScriptedDeepAgentChatModel:
    files = ["repo_onboard/analyzer.py", "tests/test_analyzer.py"]
    return ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "write_file",
                {
                    "file_path": "/artifacts/implementation_plan.md",
                    "content": _worker_plan("AST Analyzer", files),
                },
            ),
            # Intentional escape probe to verify GuardedWorktreeBackend Check #3 (`git_redirect`)
            ai_tool_call("execute", {"command": "git -C ../../main status"}),
            ai_tool_call(
                "write_file",
                {"file_path": "/repo_onboard/analyzer.py", "content": ANALYZER_CODE},
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/tests/test_analyzer.py", "content": ANALYZER_TEST},
            ),
            ai_tool_call(
                "send_team_message",
                {
                    "recipient": "worker-tutor",
                    "message": "Contract: analyze_directory() returns {total_files, archify_graph: {nodes, edges}}.",
                },
            ),
            ai_tool_call(
                "send_team_message",
                {
                    "recipient": "worker-web",
                    "message": "CodebaseAnalyzer.analyze_directory(root_path) ready on worktree-worker-analyzer.",
                },
            ),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Implement CodebaseAnalyzer & unit tests", "status": "completed"},
                        {"content": "Broadcast analyzer schema contract", "status": "completed"},
                    ]
                },
            ),
            ai_tool_call(
                "write_file",
                {
                    "file_path": "/artifacts/walkthrough.md",
                    "content": _worker_walkthrough("AST Analyzer", files),
                },
            ),
            ai_text("Completed CodebaseAnalyzer and unit tests."),
        ],
        grader_steps=[grader_verdict("satisfied", "Analyzer implementation and contract verified.")],
    )


def _make_tutor_worker_double() -> ScriptedDeepAgentChatModel:
    files = ["repo_onboard/onboarding_tutor.py", "tests/test_onboarding_tutor.py"]
    return ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "write_file",
                {
                    "file_path": "/artifacts/implementation_plan.md",
                    "content": _worker_plan("Socratic Tutor", files),
                },
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/repo_onboard/onboarding_tutor.py", "content": TUTOR_CODE},
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/tests/test_onboarding_tutor.py", "content": TUTOR_TEST},
            ),
            ai_tool_call(
                "send_team_message",
                {
                    "recipient": "worker-web",
                    "message": "Contract: SocraticOnboardingTutor exposes build_ladder(analysis) and evaluate_answer(ladder, qid, idx).",
                },
            ),
            ai_tool_call(
                "send_team_message",
                {
                    "recipient": "lead",
                    "message": "TASK-2 SocraticOnboardingTutor verified in isolated worktree.",
                },
            ),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Implement SocraticOnboardingTutor & unit tests", "status": "completed"},
                    ]
                },
            ),
            ai_tool_call(
                "write_file",
                {
                    "file_path": "/artifacts/walkthrough.md",
                    "content": _worker_walkthrough("Socratic Tutor", files),
                },
            ),
            ai_text("Completed SocraticOnboardingTutor and unit tests."),
        ],
        grader_steps=[grader_verdict("satisfied", "Tutor implementation and contract verified.")],
    )


def _make_web_worker_double() -> ScriptedDeepAgentChatModel:
    files = ["repo_onboard/service.py", "tests/test_service.py"]
    return ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call("read_team_messages", {}),
            ai_tool_call(
                "write_file",
                {
                    "file_path": "/artifacts/implementation_plan.md",
                    "content": _worker_plan("RepoOnboard Facade Service", files),
                },
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/repo_onboard/service.py", "content": SERVICE_CODE},
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/tests/test_service.py", "content": SERVICE_TEST},
            ),
            ai_tool_call(
                "send_team_message",
                {
                    "recipient": "lead",
                    "message": "TASK-3 RepoOnboardService integrated upstream analyzer + tutor contracts.",
                },
            ),
            ai_tool_call(
                "update_task_status",
                {
                    "task_id": "TASK-3",
                    "status": "in_review",
                    "note": "Facade service ready for review.",
                },
            ),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Integrate upstream contracts in RepoOnboardService", "status": "completed"},
                    ]
                },
            ),
            ai_tool_call(
                "write_file",
                {
                    "file_path": "/artifacts/walkthrough.md",
                    "content": _worker_walkthrough("RepoOnboard Facade Service", files),
                },
            ),
            ai_text("Completed RepoOnboardService facade and unit tests."),
        ],
        grader_steps=[grader_verdict("satisfied", "Service facade and tests verified.")],
    )


def test_workflow_run_endpoint_returns_503_when_llm_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("GEMINI_API_KEY", "QWEN_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    repo_root = Path(__file__).resolve().parent.parent
    settings = AppSettings(
        repo_root=repo_root,
        static_dir=repo_root / "app" / "static",
        demo_workspace=tmp_path / "demo_ws",
        internal_bare_remote=tmp_path / "origin.git",
        code_mode_sandbox=tmp_path / "sandbox",
        task_runs_dir=tmp_path / "runs",
    )
    test_app = create_app(settings=settings)
    with TestClient(test_app) as client:
        res = client.post(
            "/api/workflow/run",
            json={"goal": "Build RepoOnboard"},
        )
        assert res.status_code == 503
        body = res.json()
        assert body["error"] == LLM_CONFIG_REQUIRED_BANNER
        assert "GEMINI_API_KEY" in body["detail"]


@POSIX_ONLY
@pytest.mark.asyncio
async def test_multi_agent_worktree_build_review_merge_and_repo_onboard(tmp_path: Path) -> None:
    workspace = tmp_path / "demo_repo_onboard"
    workspace.mkdir(parents=True)
    bare_remote = tmp_path / "internal_origin.git"

    rotator = LLMProviderRotator()
    doubles = {
        "lead": (_make_lead_double(), "scripted:lead"),
        "worker-analyzer": (_make_analyzer_worker_double(), "scripted:worker-analyzer"),
        "worker-tutor": (_make_tutor_worker_double(), "scripted:worker-tutor"),
        "worker-web": (_make_web_worker_double(), "scripted:worker-web"),
    }

    def _model_factory(agent_id: str, _role: str):
        return doubles[agent_id]

    coordinator = MultiAgentTeamCoordinator(
        workspace_root=workspace,
        rotator=rotator,
        bare_remote_path=bare_remote,
        model_factory=_model_factory,
    )
    state = await coordinator.execute_full_workflow(
        "Build RepoOnboard: Local & Internal Git Codebase Onboarding Service"
    )

    # 1. Verify all 3 tasks were created by Lead DeepAgent, reviewed (100/100), and merged onto `main`
    assert state["engine"] == "langchain-ai/deepagents"
    assert state["status"] == "completed"
    assert len(state["lead_todos"]) == 2
    assert "# Team Implementation Plan" in state["team_artifacts"]["implementation_plan"]
    assert "# Team Walkthrough" in state["team_artifacts"]["walkthrough"]
    assert "PASSED" in state["team_artifacts"]["walkthrough"]

    tasks = state["tasks"]
    assert len(tasks) == 3
    for t in tasks:
        assert t["status"] == "merged"
        assert t["review_score"] == 100
    assert len(state["merged_branches"]) == 3
    assert state["internal_remote"]["configured"] is True

    # Verify bare remote received merged `main`
    ls_remote = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare_remote)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "refs/heads/main" in ls_remote.stdout

    # 2. Verify GuardedWorktreeBackend blocked Worker 1's intentional `git -C` escape attempt
    analyzer_handoff = state["handoffs"]["worker-analyzer"]
    assert analyzer_handoff["blocked_attempts_count"] == 1
    assert analyzer_handoff["blocked_attempts"][0]["check_name"] == "git_redirect"
    assert "# Implementation Plan" in analyzer_handoff["artifacts"]["implementation_plan"]
    assert "abyss:verified-gate" in analyzer_handoff["artifacts"]["walkthrough"]

    # 3. Verify Agent Teams Mailbox communication occurred (5 worker messages + 3 reviewer feedback = 8)
    assert len(state["mailbox"]) >= 6

    # 4. Verify merged files exist on `main` checkout in `workspace`
    assert (workspace / "repo_onboard" / "analyzer.py").exists()
    assert (workspace / "repo_onboard" / "onboarding_tutor.py").exists()
    assert (workspace / "repo_onboard" / "service.py").exists()

    # 5. Dynamically import the newly merged RepoOnboardService and analyze our live repo!
    sys.path.insert(0, str(workspace))
    try:
        for mod_name in list(sys.modules):
            if mod_name == "repo_onboard" or mod_name.startswith("repo_onboard."):
                sys.modules.pop(mod_name, None)

        from repo_onboard.service import RepoOnboardService

        service = RepoOnboardService()
        repo_root = Path(__file__).resolve().parent.parent
        package = service.onboard_repository(str(repo_root))

        assert package["analysis"]["total_files"] >= 10
        assert len(package["analysis"]["archify_graph"]["nodes"]) >= 10
        assert len(package["socratic_ladder"]) == 3

        first_q = package["socratic_ladder"][0]
        eval_res = service.check_socratic_answer(
            first_q["id"], selected_index=first_q["correct_index"]
        )
        assert eval_res["is_correct"] is True
    finally:
        if str(workspace) in sys.path:
            sys.path.remove(str(workspace))
        for mod_name in list(sys.modules):
            if mod_name == "repo_onboard" or mod_name.startswith("repo_onboard."):
                sys.modules.pop(mod_name, None)
