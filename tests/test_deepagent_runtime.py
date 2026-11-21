"""
Phase 3 (deepagents migration): `DeepAgentRuntime` end-to-end graph verification.

Exercises real `create_deep_agent` StateGraphs with `ScriptedDeepAgentChatModel`:
  - `write_todos` -> `execute("pytest")` fails -> `PytestGateMiddleware` repair loop -> passes
  - Guardrail refusal feedback (`[WORKTREE ISOLATION REFUSAL - ...]`) & `blocked_attempts`
  - `task` subagent delegation (`repo-explorer` read-only confinement) & `subagents=False`
  - `RubricMiddleware` revision loop (`needs_revision` -> `satisfied`) & `rubric_enabled=False`
  - Code Mode `repl` mode (`run_python` batch execution)
  - Read-only `reviewer` mode with Pydantic `response_format` structured output
  - `/AGENTS.md` memory auto-discovery
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.domains.execution import (
    CodeModeExecutor,
    DeepAgentRuntime,
    GuardedWorktreeBackend,
    ReadOnlyGuardedBackend,
)
from tests.helpers_llm_doubles import (
    ScriptedDeepAgentChatModel,
    ai_text,
    ai_tool_call,
    grader_verdict,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics (run under WSL/Linux)")


@pytest.fixture()
def buggy_worktree(tmp_path: Path) -> dict[str, Path]:
    main = tmp_path / "main"
    main.mkdir()
    (main / ".env").write_text("GEMINI_API_KEY=SECRET_DO_NOT_LEAK_999\n", encoding="utf-8")

    wt = tmp_path / "worktrees" / "builder-1"
    (wt / "src").mkdir(parents=True)
    (wt / "tests").mkdir(parents=True)
    (wt / ".git").mkdir()
    (wt / ".env").write_text("GEMINI_API_KEY=SECRET_DO_NOT_LEAK_999\n", encoding="utf-8")
    (wt / "src" / "pricing.py").write_text(
        "def subtotal(price: float, qty: int) -> float:\n"
        "    return price + qty  # BUG: should multiply\n",
        encoding="utf-8",
    )
    (wt / "tests" / "test_pricing.py").write_text(
        "from src.pricing import subtotal\n\n\n"
        "def test_subtotal() -> None:\n"
        "    assert subtotal(12.5, 4) == 50.0\n",
        encoding="utf-8",
    )
    return {"main": main, "worktree": wt}


@POSIX_ONLY
def test_pytest_gate_drives_closed_loop_self_healing(buggy_worktree: dict[str, Path]) -> None:
    backend = GuardedWorktreeBackend(buggy_worktree["worktree"], buggy_worktree["main"])
    events: list[dict] = []

    model = ScriptedDeepAgentChatModel(
        steps=[
            # Turn 1: record todos
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Inspect pricing bug", "status": "in_progress"},
                        {"content": "Fix multiplication & verify pytest", "status": "pending"},
                    ]
                },
            ),
            # Turn 2: inspect failing test via shell, then stop prematurely without fixing!
            ai_tool_call("execute", {"command": "pytest -q -p no:cacheprovider"}),
            ai_text("<think>Maybe it passes already?</think>Attempting early exit."),
            # Turn 4 (after PytestGateMiddleware injects failure HumanMessage): fix the bug!
            ai_tool_call(
                "edit_file",
                {
                    "file_path": "/src/pricing.py",
                    "old_string": "return price + qty  # BUG: should multiply",
                    "new_string": "return round(price * qty, 2)",
                },
            ),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Inspect pricing bug", "status": "completed"},
                        {"content": "Fix multiplication & verify pytest", "status": "completed"},
                    ]
                },
            ),
            ai_text("Fixed `subtotal` multiplication; pytest now passes."),
        ]
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="builder",
        workflow="fast",
        completion_cmd="pytest -q -p no:cacheprovider",
        rubric_enabled=False,
        max_gate_iterations=3,
        event_callback=events.append,
    )
    result = rt.run("You are a Python bug-fixing agent.", "Fix the subtotal bug in src/pricing.py.")

    assert result.passed is True
    assert result.status == "completed"
    assert len(result.pytest_gate_runs) == 2
    assert result.pytest_gate_runs[0].passed is False
    assert result.pytest_gate_runs[1].passed is True
    assert result.files_touched == ["src/pricing.py"]
    assert result.todos[-1] == {"content": "Fix multiplication & verify pytest", "status": "completed"}
    assert result.reasoning_trace == ["Maybe it passes already?"]
    assert any(e["event_type"] == "pytest_gate_evaluated" and e["passed"] for e in events)


@POSIX_ONLY
def test_pytest_gate_marks_failed_when_retries_exhausted(buggy_worktree: dict[str, Path]) -> None:
    backend = GuardedWorktreeBackend(buggy_worktree["worktree"], buggy_worktree["main"])
    model = ScriptedDeepAgentChatModel(
        steps=[
            ai_text("Stopping without fixing."),
            ai_text("Still not fixing."),
        ]
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="builder",
        workflow="fast",
        completion_cmd="pytest -q -p no:cacheprovider",
        rubric_enabled=False,
        max_gate_iterations=1,
    )
    result = rt.run("Fix the bug.", "Fix src/pricing.py")

    assert result.passed is False
    assert result.status == "failed"
    assert len(result.pytest_gate_runs) == 2
    assert all(not r.passed for r in result.pytest_gate_runs)


@POSIX_ONLY
def test_guardrail_refusal_feedback_is_captured_and_agent_recovers(
    buggy_worktree: dict[str, Path],
) -> None:
    backend = GuardedWorktreeBackend(buggy_worktree["worktree"], buggy_worktree["main"])
    model = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call("read_file", {"file_path": "/.env"}),
            ai_tool_call("execute", {"command": "cd app && git push origin main"}),
            ai_tool_call("read_file", {"file_path": "/src/pricing.py"}),
            ai_text("Inspected allowed source file after guardrail refusals."),
        ]
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="direct",
        completion_cmd=None,
        rubric_enabled=False,
    )
    result = rt.run("Inspect safely.", "Read pricing module.")

    assert result.passed is True
    assert len(result.blocked_attempts) == 2
    statuses = [(t.tool_name, t.status) for t in result.turn_trace]
    assert statuses == [
        ("read_file", "blocked"),
        ("execute", "blocked"),
        ("read_file", "ok"),
    ]


def test_task_subagent_delegation_is_read_only_and_recorded(
    buggy_worktree: dict[str, Path],
) -> None:
    backend = GuardedWorktreeBackend(buggy_worktree["worktree"], buggy_worktree["main"])
    model = ScriptedDeepAgentChatModel(
        steps=[
            # Parent delegates exploration to `repo-explorer`
            ai_tool_call(
                "task",
                {
                    "subagent_type": "repo-explorer",
                    "description": "Find how subtotal is implemented in /src/pricing.py",
                },
            ),
            # Inside `repo-explorer` subagent: read works, write is denied by subagent permissions!
            ai_tool_call("read_file", {"file_path": "/src/pricing.py"}),
            ai_tool_call("write_file", {"file_path": "/src/pricing.py", "content": "hacked"}),
            ai_text("`/src/pricing.py` line 2 adds `price + qty` instead of multiplying."),
            # Back in parent agent with subagent report:
            ai_text("Explorer confirmed `/src/pricing.py` line 2 has an addition bug."),
        ]
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="direct",
        completion_cmd=None,
        rubric_enabled=False,
        subagents=True,
    )
    result = rt.run("Coordinate exploration.", "Locate the pricing bug.")

    assert result.passed is True
    assert len(result.subagent_calls) == 1
    assert result.subagent_calls[0].subagent_type == "repo-explorer"
    assert "instead of multiplying" in result.subagent_calls[0].response_preview
    assert "addition bug" in result.summary
    assert "price + qty" in (buggy_worktree["worktree"] / "src" / "pricing.py").read_text(
        encoding="utf-8"
    )


def test_subagents_disabled_hides_task_tool(buggy_worktree: dict[str, Path]) -> None:
    backend = GuardedWorktreeBackend(buggy_worktree["worktree"], buggy_worktree["main"])
    model = ScriptedDeepAgentChatModel(steps=[ai_text("Done without subagents.")])

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="direct",
        completion_cmd=None,
        rubric_enabled=False,
        subagents=False,
    )
    result = rt.run("Direct mode.", "Say hello.")

    assert result.passed is True
    assert "task" not in model.calls[0].bound_tools


def test_rubric_middleware_requests_revision_then_accepts(
    buggy_worktree: dict[str, Path],
) -> None:
    backend = GuardedWorktreeBackend(buggy_worktree["worktree"], buggy_worktree["main"])
    model = ScriptedDeepAgentChatModel(
        steps=[
            ai_text("Fixed pricing."),
            # After rubric grader asks for concrete test evidence in summary:
            ai_text("Fixed `subtotal` in `src/pricing.py` to return `round(price * qty, 2)`."),
        ],
        grader_steps=[
            grader_verdict(
                "needs_revision",
                "Summary did not name the modified function.",
                criteria=[
                    {
                        "name": "Final answer names the modified function and formula",
                        "passed": False,
                        "gap": "Name `subtotal` and `round(price * qty, 2)` explicitly.",
                    }
                ],
            ),
            grader_verdict(
                "satisfied",
                "Summary now names `subtotal` and the exact formula.",
                criteria=[
                    {
                        "name": "Final answer names the modified function and formula",
                        "passed": True,
                    }
                ],
            ),
        ],
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="direct",
        completion_cmd=None,
        rubric="Final answer must name the modified function and formula.",
        rubric_enabled=True,
    )
    result = rt.run("Summarize the fix.", "Explain the subtotal fix.")

    assert result.passed is True
    assert result.rubric_status == "satisfied"
    assert len(result.rubric_evaluations) == 2
    assert result.rubric_evaluations[0]["result"] == "needs_revision"
    assert result.rubric_evaluations[1]["result"] == "satisfied"


@POSIX_ONLY
def test_repl_mode_executes_batch_python_via_code_mode(
    buggy_worktree: dict[str, Path],
) -> None:
    wt = buggy_worktree["worktree"]
    backend = GuardedWorktreeBackend(wt, buggy_worktree["main"])
    code_mode = CodeModeExecutor(worktree_root=wt, main_checkout_root=buggy_worktree["main"])

    snippet = (
        'before = await read("src/pricing.py")\n'
        'await edit("src/pricing.py", "price + qty  # BUG: should multiply", "round(price * qty, 2)")\n'
        '{"had_bug": "price + qty" in before, "files": await ls("src")}\n'
    )
    model = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call("run_python", {"code": snippet}),
            ai_text("Patched `src/pricing.py` in a single Code Mode REPL batch."),
        ]
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="repl",
        code_mode=code_mode,
        completion_cmd=None,
        rubric_enabled=False,
    )
    result = rt.run("Use Code Mode.", "Fix pricing.py via REPL.")

    assert result.passed is True
    assert "run_python" in model.calls[0].bound_tools
    assert result.turn_trace[0].tool_name == "run_python"
    assert '"had_bug": true' in result.turn_trace[0].observation.lower()
    assert "src/pricing.py" in result.files_touched
    assert "round(price * qty, 2)" in (wt / "src" / "pricing.py").read_text(encoding="utf-8")


class _SampleReviewOutput(BaseModel):
    verdict: str
    finding_count: int


def test_reviewer_mode_with_readonly_backend_and_structured_output(
    buggy_worktree: dict[str, Path],
) -> None:
    wt = buggy_worktree["worktree"]
    (wt / "AGENTS.md").write_text("# Repo Rules\nAlways multiply unit price by qty.\n", encoding="utf-8")
    ro_backend = ReadOnlyGuardedBackend(wt, buggy_worktree["main"])

    model = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call("read_file", {"file_path": "/src/pricing.py"}),
            ai_tool_call("write_file", {"file_path": "/src/pricing.py", "content": "disallow"}),
            ai_tool_call("_SampleReviewOutput", {"verdict": "changes_requested", "finding_count": 1}),
        ]
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=ro_backend,
        mode="reviewer",
        response_format=_SampleReviewOutput,
        rubric_enabled=False,
        subagents=False,
    )
    result = rt.run("Review the branch.", "Audit /src/pricing.py")

    assert result.passed is True
    assert "execute" not in model.calls[0].bound_tools
    # `/AGENTS.md` memory was discovered and injected into the system message
    sys_text = str(model.calls[0].messages[0].content)
    assert "Always multiply unit price by qty." in sys_text
    assert isinstance(result.structured_response, _SampleReviewOutput)
    assert result.structured_response.verdict == "changes_requested"
    assert result.structured_response.finding_count == 1
    assert len(result.blocked_attempts) == 1
