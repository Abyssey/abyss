from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_web_server_endpoints() -> None:
    with TestClient(app) as client:
        # 0. Pod liveness & readiness probes
        res_health = client.get("/healthz")
        assert res_health.status_code == 200
        assert res_health.json()["status"] == "ok"

        res_ready = client.get("/readyz")
        assert res_ready.status_code == 200
        ready_data = res_ready.json()
        assert ready_data["status"] == "ready"
        assert ready_data["checks"]["git"] is True
        assert ready_data["checks"]["static_ui"] is True
        assert ready_data["checks"]["internal_bare_remote"] is True

        # 1. Index HTML & Air-Gap / Sandbox Security Assertions
        res_index = client.get("/")
        assert res_index.status_code == 200
        assert "Modern Coding Agent Architecture" in res_index.text
        assert ("2026" + ".11") not in res_index.text
        assert 'sandbox="allow-scripts"' in res_index.text
        assert ("allow-" + "same-origin") not in res_index.text
        assert "이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘" in res_index.text
        assert "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘" in res_index.text
        assert "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘" in res_index.text
        assert "방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘" in res_index.text

        # Static stylesheet must be 100% air-gap safe (zero Google Fonts CDN)
        css_text = (REPO_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in css_text
        js_text = (REPO_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        assert ("allow-" + "same-origin") not in js_text
        assert "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘" in js_text

        # 2. State endpoint (includes status, merged_branches, internal_remote)
        res_state = client.get("/api/state")
        assert res_state.status_code == 200
        state_data = res_state.json()
        assert "worktrees" in state_data
        assert "rotator" in state_data
        assert "deepagents_preview" in state_data
        assert "status" in state_data
        assert "merged_branches" in state_data
        assert "internal_remote" in state_data

        # 3. Architecture Core Catalog endpoint (5 core modules + 4 supporting modules, Focus Domain framing)
        res_core = client.get("/api/learning-core")
        assert res_core.status_code == 200
        core_data = res_core.json()
        assert len(core_data["core_modules"]) == 5
        assert len(core_data["infra_modules"]) == 4
        core_blob = str(core_data)
        assert "★ Focus Domain #1" in core_blob
        assert "★ Focus Domain #2" in core_blob
        assert ("직접" + " 작성") not in core_blob
        assert ("숙" + "제") not in core_blob

        # 4. Workflow task presets endpoint
        res_presets = client.get("/api/workflow/tasks/presets")
        assert res_presets.status_code == 200
        presets_data = res_presets.json()
        assert len(presets_data["storyline_prompts"]) == 5
        assert presets_data["build_presets"][0]["id"] == "project_map_app"

        # 5. Provider rotation config & test
        res_rot = client.post(
            "/api/providers/config",
            json={"strategy": "role_based", "active_single_provider": "mock"},
        )
        assert res_rot.status_code == 200
        assert res_rot.json()["rotator"]["strategy"] == "role_based"

        res_test_prov = client.post("/api/providers/test/mock")
        assert res_test_prov.status_code == 200
        assert res_test_prov.json()["ok"] is True

        # 6. Code Mode scenarios & live REPL execution (Act 2-C ok + Act 3 refused)
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

        # Act 3 Guardrail Refusal: compound command bypass & runtime path escape
        res_cm_compound = client.post(
            "/api/code-mode/run",
            json={
                "code": 'await sh("cd \'app\' && FOO=1 git push origin main")',
                "scenario_id": "compound_cmd_trap",
                "reset_sandbox": True,
            },
        )
        assert res_cm_compound.status_code == 200
        assert res_cm_compound.json()["result"]["status"] == "refused"

        res_cm_escape = client.post(
            "/api/code-mode/run",
            json={
                "code": 'parts = ["..", "..", ".env"]\nawait write("/".join(parts), "LEAKED=1")',
                "scenario_id": "runtime_escape_and_hook",
                "reset_sandbox": True,
            },
        )
        assert res_cm_escape.status_code == 200
        assert res_cm_escape.json()["result"]["status"] == "refused"

        # 7. RepoOnboard analyze (self & internal-origin) & Socratic answer
        res_analyze_internal = client.post(
            "/api/repo-onboard/analyze",
            json={"repo_url_or_path": "internal-origin"},
        )
        assert res_analyze_internal.status_code == 200
        assert res_analyze_internal.json()["analysis"]["source_type"] == "internal_git_bare"

        res_analyze = client.post(
            "/api/repo-onboard/analyze",
            json={"repo_url_or_path": "self"},
        )
        assert res_analyze.status_code == 200
        analyze_data = res_analyze.json()
        assert analyze_data["analysis"]["source_type"] == "local_path"
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
                "reflection": "Worktree 격리 정책은 app/domains/guardrails/isolation_policy.py 에서 담당합니다.",
            },
        )
        assert res_answer.status_code == 200
        answer_data = res_answer.json()
        assert answer_data["is_correct"] is True
        assert "coach_response" in answer_data
