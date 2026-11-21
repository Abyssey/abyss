"""
Unit tests for Multi-Provider LLM Rotator (`app/core/llm/provider_rotator.py`).
Tests role_based, round_robin, single, fail-fast HTTP 503 (`LLMConfigurationError`),
status banner, builder mapping, and key masking (`mock` provider removed).
"""

from __future__ import annotations

import pytest

from app.core.llm.provider_rotator import LLMConfigurationError, LLMProviderRotator


def test_provider_rotator_strategies_and_fail_fast_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("GEMINI_API_KEY", "QWEN_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for flag in ("GEMINI_ENABLED", "QWEN_ENABLED", "OPENAI_ENABLED"):
        monkeypatch.setenv(flag, "true")

    rotator = LLMProviderRotator()
    assert "mock" not in rotator.providers

    # Default role_based mapping includes builder role
    assert rotator.role_mapping["builder"] == "qwen"

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

    # 3. Fail-Fast (`LLMConfigurationError`) when no live credentials are configured
    assert rotator.has_live_credentials() is False
    state = rotator.get_state()
    assert state["has_live_credentials"] is False
    assert "⛔ LLM CONFIG REQUIRED" in state["status_banner"]

    with pytest.raises(LLMConfigurationError) as exc_info:
        rotator.build_chat_model("worker-analyzer", "worker")
    assert exc_info.value.http_status_code == 503
    assert "⛔ LLM CONFIG REQUIRED" in str(exc_info.value)


def test_round_robin_and_api_key_masking() -> None:
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
    assert rotator.has_live_credentials() is True
    first = rotator.select_candidate_chain("worker-1", "worker")[0].provider_id
    second = rotator.select_candidate_chain("worker-1", "worker")[0].provider_id
    assert first != second

    public_state = rotator.get_state()
    assert public_state["has_live_credentials"] is True
    assert "🟢 LIVE LLM" in public_state["status_banner"]
    assert "123456789" not in public_state["providers"]["qwen"]["api_key_masked"]
    assert "..." in public_state["providers"]["qwen"]["api_key_masked"]
