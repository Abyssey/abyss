"""
Phase 4 (deepagents migration): `POST /api/code-mode/agent` Execution API tests.

Verifies:
  1. `mode="repl"`: executes natural-language instruction inside Code Mode sandbox using
     `DeepAgentRuntime` + `run_python` batch execution and passes `completion_cmd`.
  2. `mode="direct"`: executes with built-in deepagents tools + `GuardedWorktreeBackend`
     while recording guardrail refusals.
  3. Missing LLM credentials return HTTP 503 (`⛔ LLM CONFIG REQUIRED`).
  4. Offline `POST /api/code-mode/run` guardrail scenarios still work with zero LLM config.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.core.llm import LLM_CONFIG_REQUIRED_BANNER
from app.main import create_app
from tests.helpers_llm_doubles import ScriptedDeepAgentChatModel, ai_text, ai_tool_call

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics (run under WSL/Linux)")


def _make_isolated_app(tmp_path: Path):
    settings = load_settings(
        repo_root=Path(__file__).resolve().parent.parent,
        demo_workspace=tmp_path / "demo_workspace" / "repo_onboard",
        internal_bare_remote=tmp_path / "demo_workspace" / "internal_origin.git",
    )
    application = create_app(settings=settings)
    # Clear any local `.env` credentials so tests are 100% deterministic
    for pid in ("qwen", "gemini", "openai"):
        cfg = application.state.container.rotator.providers[pid]
        cfg.enabled = False
        cfg.api_key = ""
    return application


def test_code_mode_agent_returns_503_when_no_llm_credentials(tmp_path: Path) -> None:
    application = _make_isolated_app(tmp_path)
    client = TestClient(application)

    resp = client.post(
        "/api/code-mode/agent",
        json={"instruction": "Raise maxAttempts to 5 in retry_policy.py", "mode": "repl"},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "llm_config_required"
    assert body["error"] == LLM_CONFIG_REQUIRED_BANNER

    # Blank instruction is rejected by request schema validation (HTTP 422)
    bad = client.post("/api/code-mode/agent", json={"instruction": "   ", "mode": "direct"})
    assert bad.status_code == 422


@POSIX_ONLY
def test_code_mode_run_guardrail_scenario_still_works_without_llm(tmp_path: Path) -> None:
    application = _make_isolated_app(tmp_path)
    client = TestClient(application)

    resp = client.post(
        "/api/code-mode/run",
        json={
            "code": 'await sh("cd \'app\' && FOO=1 git push origin main")',
            "scenario_id": "compound_cmd_trap",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["status"] == "refused"


@POSIX_ONLY
def test_code_mode_agent_repl_mode_patches_sandbox_and_passes_pytest_gate(tmp_path: Path) -> None:
    application = _make_isolated_app(tmp_path)
    snippet = (
        'await edit("src/retry_policy.py", \'"maxAttempts": 3\', \'"maxAttempts": 5\')\n'
        'tests = await sh("pytest -q -p no:cacheprovider src/test_retry.py", nothrow=True)\n'
        '{"passed": tests.ok, "exit_code": tests.exit_code}\n'
    )
    scripted = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call("run_python", {"code": snippet}),
            ai_text("Updated `maxAttempts` to 5 and verified `src/test_retry.py` in 1 REPL turn."),
        ]
    )
    application.state.container.code_mode_service.model_factory = (
        lambda _aid, _role: (scripted, "scripted:repl-double")
    )

    client = TestClient(application)
    resp = client.post(
        "/api/code-mode/agent",
        json={
            "instruction": "Update maxAttempts to 5 in src/retry_policy.py and verify tests.",
            "mode": "repl",
            "scenario_id": "retry_policy",
            "completion_cmd": "pytest -q -p no:cacheprovider src/test_retry.py",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["result"]["status"] == "completed"
    assert payload["result"]["passed"] is True
    assert payload["result"]["provider_used"] == "scripted:repl-double"
    assert payload["comparison"]["mode"] == "repl"
    assert payload["comparison"]["inner_repl_tool_calls"] == 2
    assert "src/retry_policy.py" in payload["comparison"]["files_touched"]


@POSIX_ONLY
def test_code_mode_agent_direct_mode_records_guardrail_blocks_and_completes(tmp_path: Path) -> None:
    application = _make_isolated_app(tmp_path)
    scripted = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call("write_file", {"file_path": "/../../.env", "content": "LEAK=1"}),
            ai_tool_call(
                "edit_file",
                {
                    "file_path": "/src/retry_policy.py",
                    "old_string": '"maxAttempts": 3',
                    "new_string": '"maxAttempts": 5',
                },
            ),
            ai_text("Updated `src/retry_policy.py` directly after guardrail block."),
        ]
    )
    application.state.container.code_mode_service.model_factory = (
        lambda _aid, _role: (scripted, "scripted:direct-double")
    )

    client = TestClient(application)
    resp = client.post(
        "/api/code-mode/agent",
        json={
            "instruction": "Set maxAttempts to 5 in src/retry_policy.py.",
            "mode": "direct",
            "completion_cmd": "pytest -q -p no:cacheprovider src/test_retry.py",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["result"]["status"] == "completed"
    assert payload["result"]["passed"] is True
    assert payload["comparison"]["mode"] == "direct"
    assert payload["comparison"]["blocked_attempts_count"] == 1
    assert payload["comparison"]["files_touched"] == ["src/retry_policy.py"]
