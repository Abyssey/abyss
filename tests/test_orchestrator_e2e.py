"""
End-to-End Integration Test for MultiAgentTeamCoordinator (`abyss/orchestrator/team_coordinator.py`).
Verifies:
1. Lead Agent decomposes RepoOnboard into 3 tasks (.abyss/tasks.json)
2. 3 isolated Git Worktrees are created and locked (.abyss/worktrees/worker-*)
3. Worker 1 triggers and records a WorktreeIsolationPolicy guard block (`git -C` refusal) before writing code
4. Workers exchange schema contracts via Agent Mailbox (.abyss/mailbox/*.jsonl)
5. Reviewer Agent runs `EvaluatorRubric` + `pytest` on each worktree branch and approves all 3
6. Lead Agent merges all 3 worktree branches onto `main`
7. The merged `RepoOnboardService` actually analyzes our own repository (`abyss`) and runs Socratic Q&A!
"""

from pathlib import Path
import sys
import pytest

from abyss.llm.provider_rotator import LLMProviderRotator
from abyss.orchestrator.team_coordinator import MultiAgentTeamCoordinator


@pytest.mark.asyncio
async def test_multi_agent_worktree_build_review_merge_and_repo_onboard(tmp_path: Path) -> None:
    workspace = tmp_path / "demo_repo_onboard"
    workspace.mkdir(parents=True)

    rotator = LLMProviderRotator()
    rotator.update_config({"strategy": "single", "active_single_provider": "mock"})

    coordinator = MultiAgentTeamCoordinator(workspace_root=workspace, rotator=rotator)
    state = await coordinator.execute_full_workflow(
        "Build RepoOnboard: GitLab/GitHub Codebase Onboarding & Socratic Q&A Service"
    )

    # 1. Verify all 3 tasks were created, reviewed (100/100), and merged onto `main`
    tasks = state["tasks"]
    assert len(tasks) == 3
    for t in tasks:
        assert t["status"] == "merged"
        assert t["review_score"] == 100

    # 2. Verify WorktreeIsolationPolicy blocked Worker 1's intentional `git -C` escape attempt
    analyzer_handoff = state["handoffs"]["worker-analyzer"]
    assert analyzer_handoff["blocked_attempts"] == 1
    blocked_records = [r for r in analyzer_handoff["tool_history"] if r["blocked"]]
    assert len(blocked_records) == 1
    assert blocked_records[0]["check_name"] == "git_redirect"

    # 3. Verify Agent Teams Mailbox communication occurred
    assert len(state["mailbox"]) >= 6  # 3 worker schema messages + 3 reviewer verdicts

    # 4. Verify merged files exist on `main` checkout in `workspace`
    assert (workspace / "repo_onboard" / "analyzer.py").exists()
    assert (workspace / "repo_onboard" / "onboarding_tutor.py").exists()
    assert (workspace / "repo_onboard" / "service.py").exists()

    # 5. Dynamically import the newly merged RepoOnboardService and analyze our actual Abyss codebase!
    sys.path.insert(0, str(workspace))
    try:
        from repo_onboard.service import RepoOnboardService

        service = RepoOnboardService()
        abyss_root = Path(__file__).resolve().parent.parent
        package = service.onboard_repository(str(abyss_root))

        assert package["repo_name"] == "abyss"
        assert package["analysis"]["total_files"] >= 6
        assert len(package["analysis"]["archify_graph"]["nodes"]) >= 6
        assert len(package["socratic_ladder"]) == 3

        first_q = package["socratic_ladder"][0]
        eval_res = service.check_socratic_answer(
            first_q["id"], selected_index=first_q["correct_index"]
        )
        assert eval_res["is_correct"] is True
    finally:
        sys.path.remove(str(workspace))
