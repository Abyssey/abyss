"""
Phase 3 (deepagents migration): Antigravity 3-phase `ArtifactWorkflowMiddleware` tests.

Verifies on real `create_deep_agent` graphs:
  1. `[PLAN-FIRST REFUSAL]` blocks workspace edits and `execute` during PLANNING until
     `/artifacts/implementation_plan.md` is written.
  2. `/artifacts/*` files are routed via `CompositeBackend` outside the git worktree.
  3. `write_todos` is deterministically rendered to `/artifacts/task.md` (`[ ]` / `[/]` / `[x]`).
  4. Missing `/artifacts/walkthrough.md` triggers a follow-up turn without re-running the
     rubric grader, and `Validation Results` is deterministically stamped with actual `pytest`
     gate output.
  5. `plan_review="request_review"` (the default) pauses via LangGraph `interrupt()`, supports
     plan-revision feedback (`proceed=False`), and resumes execution with user review comments
     (`proceed=True`).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.domains.execution import DeepAgentRuntime, GuardedWorktreeBackend
from app.domains.execution.artifact_workflow import PLAN_FIRST_REFUSAL_PREFIX
from tests.helpers_llm_doubles import (
    ScriptedDeepAgentChatModel,
    ai_text,
    ai_tool_call,
    grader_verdict,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics (run under WSL/Linux)")

SAMPLE_PLAN_MD = """\
# Implementation Plan

## Goal
Fix the multiplication bug in `src/pricing.py`.

## User Review Required
None.

## Proposed Changes
- [MODIFY] `src/pricing.py`: multiply `price * qty` instead of adding.

## Verification Plan
- Run `pytest -q -p no:cacheprovider`.
"""

SAMPLE_WALKTHROUGH_MD = """\
# Walkthrough

## Changes Made
- Updated `subtotal` in `src/pricing.py` to multiply `price * qty` rounded to 2 decimals.

## Validation Results
- Unit test `tests/test_pricing.py` passed.
"""


@pytest.fixture()
def project(tmp_path: Path) -> dict[str, Path]:
    main = tmp_path / "main"
    main.mkdir()
    wt = tmp_path / "worktrees" / "worker-1"
    (wt / "src").mkdir(parents=True)
    (wt / "tests").mkdir(parents=True)
    (wt / "src" / "pricing.py").write_text(
        "def subtotal(price: float, qty: int) -> float:\n    return price + qty\n",
        encoding="utf-8",
    )
    (wt / "tests" / "test_pricing.py").write_text(
        "from src.pricing import subtotal\n\n\ndef test_subtotal() -> None:\n    assert subtotal(10.0, 3) == 30.0\n",
        encoding="utf-8",
    )
    artifacts_dir = tmp_path / "data" / "tasks" / "task-001" / "artifacts"
    return {"main": main, "worktree": wt, "artifacts_dir": artifacts_dir}


@POSIX_ONLY
def test_plan_first_refusal_task_rendering_walkthrough_retry_and_pytest_stamp(
    project: dict[str, Path],
) -> None:
    wt = project["worktree"]
    artifacts_dir = project["artifacts_dir"]
    backend = GuardedWorktreeBackend(wt, project["main"])

    model = ScriptedDeepAgentChatModel(
        steps=[
            # 1) Premature write & shell command during PLANNING -> both must be refused!
            ai_tool_call("write_file", {"file_path": "/src/pricing.py", "content": "premature"}),
            ai_tool_call("execute", {"command": "pytest -q -p no:cacheprovider"}),
            # 2) Read-only exploration is allowed during PLANNING
            ai_tool_call("read_file", {"file_path": "/src/pricing.py"}),
            # 3) Write `/artifacts/implementation_plan.md` -> transitions to EXECUTION (`always_proceed`)
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/implementation_plan.md", "content": SAMPLE_PLAN_MD},
            ),
            # 4) In EXECUTION: track todos -> edit code -> mark todos completed -> forget walkthrough!
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Read pricing.py", "status": "completed"},
                        {"content": "Fix subtotal multiplication", "status": "in_progress"},
                        {"content": "Write walkthrough", "status": "pending"},
                    ]
                },
            ),
            ai_tool_call(
                "edit_file",
                {
                    "file_path": "/src/pricing.py",
                    "old_string": "return price + qty",
                    "new_string": "return round(price * qty, 2)",
                },
            ),
            ai_text("Code is fixed and ready!"),
            # 5) PytestGate passes -> RubricMiddleware passes -> WalkthroughGate prompts for walkthrough.md!
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Read pricing.py", "status": "completed"},
                        {"content": "Fix subtotal multiplication", "status": "completed"},
                        {"content": "Write walkthrough", "status": "completed"},
                    ]
                },
            ),
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/walkthrough.md", "content": SAMPLE_WALKTHROUGH_MD},
            ),
            ai_text("Walkthrough recorded."),
        ],
        grader_steps=[
            grader_verdict("satisfied", "Code fix satisfies the rubric."),
        ],
    )

    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="builder",
        workflow="planning",
        plan_review="always_proceed",
        artifacts_dir=artifacts_dir,
        completion_cmd="pytest -q -p no:cacheprovider",
        rubric="Fix the subtotal calculation so unit tests pass.",
        rubric_enabled=True,
    )
    result = rt.run("Build cleanly.", "Fix subtotal in src/pricing.py")

    assert result.passed is True
    assert result.status == "completed"

    # Both premature actions were refused with `[PLAN-FIRST REFUSAL]`
    assert len(result.plan_refusals) == 2
    assert result.turn_trace[0].status == "blocked"
    assert result.turn_trace[0].observation.startswith(PLAN_FIRST_REFUSAL_PREFIX)
    assert result.turn_trace[1].status == "blocked"
    assert result.turn_trace[1].observation.startswith(PLAN_FIRST_REFUSAL_PREFIX)

    # Artifacts exist in `artifacts_dir` OUTSIDE the git worktree
    assert not (wt / "artifacts").exists()
    assert (artifacts_dir / "implementation_plan.md").is_file()
    assert (artifacts_dir / "task.md").is_file()
    assert (artifacts_dir / "walkthrough.md").is_file()

    assert "## Proposed Changes" in result.artifacts["implementation_plan"]
    assert "- [x] Fix subtotal multiplication" in result.artifacts["task"]
    assert "- [x] Write walkthrough" in result.artifacts["task"]

    # Deterministic pytest output was stamped into `walkthrough.md` Validation Results
    walkthrough_text = result.artifacts["walkthrough"]
    assert "### Verified Command Output (Deterministic Gate)" in walkthrough_text
    assert "Command: `pytest -q -p no:cacheprovider`" in walkthrough_text
    assert "Exit Code: `0`" in walkthrough_text
    assert "1 passed" in walkthrough_text

    # Rubric grader ran ONCE (not re-invoked on the walkthrough-only follow-up turn!)
    assert len(result.rubric_evaluations) == 1
    assert len(result.pytest_gate_runs) == 1


@POSIX_ONLY
def test_request_review_interrupts_supports_plan_revision_and_resumes_with_comments(
    project: dict[str, Path],
) -> None:
    wt = project["worktree"]
    artifacts_dir = project["artifacts_dir"]
    backend = GuardedWorktreeBackend(wt, project["main"])

    revised_plan = SAMPLE_PLAN_MD.replace(
        "multiply `price * qty` instead of adding.",
        "multiply `price * qty` and round to 2 decimal places per reviewer comment.",
    )

    def _edit_after_checking_comment_injection(messages, _bound_tools):
        # Verify the reviewer's Proceed comment was injected into both the tool result & system prompt!
        combined = "\n".join(str(m.content) for m in messages)
        assert "Round result to 2 decimals" in combined
        return ai_tool_call(
            "edit_file",
            {
                "file_path": "/src/pricing.py",
                "old_string": "return price + qty",
                "new_string": "return round(price * qty, 2)",
            },
        )

    model = ScriptedDeepAgentChatModel(
        steps=[
            # 1) Agent writes initial plan -> `request_review` interrupts immediately!
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/implementation_plan.md", "content": SAMPLE_PLAN_MD},
            ),
            # 2) User responds with `proceed=False` + revision request -> still in PLANNING!
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/implementation_plan.md", "content": revised_plan},
            ),
            # 3) User clicks Proceed with inline comment -> now in EXECUTION!
            _edit_after_checking_comment_injection,
            ai_tool_call(
                "write_file",
                {"file_path": "/artifacts/walkthrough.md", "content": SAMPLE_WALKTHROUGH_MD},
            ),
            ai_text("Implemented approved plan with 2-decimal rounding."),
        ]
    )

    # Note: `plan_review` omitted -> defaults to `"request_review"`!
    rt = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="builder",
        artifacts_dir=artifacts_dir,
        completion_cmd="pytest -q -p no:cacheprovider",
        rubric_enabled=False,
    )

    paused = rt.run("Plan then code.", "Fix subtotal in src/pricing.py", thread_id="task-review-1")
    assert paused.status == "awaiting_plan_review"
    assert paused.awaiting_plan_review is True
    assert paused.passed is False
    assert "# Implementation Plan" in paused.artifacts["implementation_plan"]
    # Workspace has NOT been modified while awaiting review
    assert "price + qty" in (wt / "src" / "pricing.py").read_text(encoding="utf-8")

    # Reviewer asks to revise the plan first (`proceed=False`)
    paused_again = rt.resume(
        thread_id="task-review-1",
        proceed=False,
        comments="Mention 2-decimal rounding in Proposed Changes.",
    )
    assert paused_again.status == "awaiting_plan_review"
    assert "round to 2 decimal places per reviewer comment" in paused_again.artifacts["implementation_plan"]

    # Reviewer approves (`Proceed`) with an inline implementation comment
    completed = rt.resume(
        thread_id="task-review-1",
        proceed=True,
        comments="Round result to 2 decimals.",
    )
    assert completed.status == "completed"
    assert completed.passed is True
    assert completed.awaiting_plan_review is False
    assert "round(price * qty, 2)" in (wt / "src" / "pricing.py").read_text(encoding="utf-8")
    assert "Exit Code: `0`" in completed.artifacts["walkthrough"]
