from __future__ import annotations

from fastapi.testclient import TestClient

from abyss.web.server import app


def test_web_server_endpoints() -> None:
    with TestClient(app) as client:
        # 1. Index HTML
        res_index = client.get("/")
        assert res_index.status_code == 200
        assert "Abyss — Claude Code Architecture" in res_index.text
        assert "6. Code Mode (REPL · Ctrl+O) Lab" in res_index.text

        # 2. State endpoint
        res_state = client.get("/api/state")
        assert res_state.status_code == 200
        state_data = res_state.json()
        assert "worktrees" in state_data
        assert "rotator" in state_data
        assert "deepagents_preview" in state_data

        # 3. Learning Core endpoint (5 user-owned core modules + 4 infra modules)
        res_core = client.get("/api/learning-core")
        assert res_core.status_code == 200
        core_data = res_core.json()
        assert len(core_data["core_modules"]) == 5
        assert len(core_data["infra_modules"]) == 4

        # 4. Provider rotation config & test
        res_rot = client.post(
            "/api/providers/config",
            json={"strategy": "role_based", "active_single_provider": "mock"},
        )
        assert res_rot.status_code == 200
        assert res_rot.json()["rotator"]["strategy"] == "role_based"

        res_test_prov = client.post("/api/providers/test/mock")
        assert res_test_prov.status_code == 200
        assert res_test_prov.json()["ok"] is True

        # 5. Code Mode scenarios & live REPL execution endpoint
        res_cm_scenarios = client.get("/api/code-mode/scenarios")
        assert res_cm_scenarios.status_code == 200
        scenarios = res_cm_scenarios.json()["scenarios"]
        assert len(scenarios) == 4

        res_cm_run = client.post(
            "/api/code-mode/run",
            json={
                "code": scenarios[0]["python_code"],
                "scenario_id": scenarios[0]["id"],
                "reset_sandbox": True,
            },
        )
        assert res_cm_run.status_code == 200
        cm_data = res_cm_run.json()
        assert cm_data["result"]["status"] == "ok"
        assert cm_data["result"]["return_value"]["edited"] == "src/retry_policy.py"
        assert cm_data["result"]["return_value"]["tests"]["passed"] is True
        assert cm_data["comparison"]["code_mode_llm_turns"] == 1

        # 6. RepoOnboard analyze & Socratic answer
        res_analyze = client.post(
            "/api/repo-onboard/analyze",
            json={"repo_url_or_path": "self"},
        )
        assert res_analyze.status_code == 200
        analyze_data = res_analyze.json()
        assert "analysis" in analyze_data
        assert "archify_graph" in analyze_data["analysis"]
        assert "mermaid" in analyze_data["analysis"]
        assert len(analyze_data["socratic_ladder"]) == 3

        first_q = analyze_data["socratic_ladder"][0]
        res_answer = client.post(
            "/api/repo-onboard/answer",
            json={
                "repo_url_or_path": "self",
                "question_id": first_q["id"],
                "selected_index": first_q["correct_index"],
                "reflection": "Worktree 격리 정책은 isolation_policy.py 와 worktree_manager.py 에서 담당합니다.",
            },
        )
        assert res_answer.status_code == 200
        answer_data = res_answer.json()
        assert answer_data["is_correct"] is True
        assert "coach_response" in answer_data
