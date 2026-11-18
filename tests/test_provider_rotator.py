"""
Unit tests for Multi-Provider LLM Rotator (`abyss/llm/provider_rotator.py`).
Tests role_based, round_robin, single, and fallback strategies.
"""

from abyss.llm.provider_rotator import LLMProviderRotator, RotationStrategy


def test_provider_rotator_strategies_and_live_reconfig() -> None:
    rotator = LLMProviderRotator()

    # 1. Role-based routing
    rotator.update_config(
        {
            "strategy": "role_based",
            "role_mapping": {
                "lead": "gemini",
                "worker-analyzer": "qwen",
                "reviewer": "openai",
            },
        }
    )
    chain_worker = rotator.select_candidate_chain("worker-analyzer", "worker")
    assert chain_worker[0].provider_id == "qwen"

    chain_lead = rotator.select_candidate_chain("lead", "lead")
    assert chain_lead[0].provider_id == "gemini"

    # 2. Single provider routing
    rotator.update_config({"strategy": "single", "active_single_provider": "qwen"})
    chain_single = rotator.select_candidate_chain("lead", "lead")
    assert chain_single[0].provider_id == "qwen"

    # 3. Automatic fallback to mock when credentials are not yet configured
    res = rotator.invoke_chat(
        agent_id="worker-analyzer",
        role="worker",
        messages=[{"role": "user", "content": "Hello"}],
        mock_responder=lambda _m, _t: {"content": "<think>plan</think>Done", "tool_calls": []},
    )
    assert "Done" in res["content"]
    assert res["fallback_triggered"] is True


def test_round_robin_rotates_across_ready_providers() -> None:
    rotator = LLMProviderRotator()
    rotator.update_config(
        {
            "strategy": "round_robin",
            "providers": {
                "qwen": {"api_key": "sk-real-qwen-key-123456789", "enabled": True},
                "gemini": {"api_key": "AIzaSyRealGeminiKey123456789", "enabled": True},
            },
        }
    )
    first = rotator.select_candidate_chain("worker-1", "worker")[0].provider_id
    second = rotator.select_candidate_chain("worker-1", "worker")[0].provider_id
    assert first != second
