"""
Phase 1 (deepagents migration): LangChain chat-model factory & 503 Fail-Fast.

`LLMProviderRotator.build_chat_model` must
  - build real LangChain chat models (no network call at construction time),
  - follow the rotation chain while skipping mock / disabled / credential-less providers,
  - expose fallbacks as `ModelFallbackMiddleware` (not `.with_fallbacks`, which loses `bind_tools`),
  - raise `LLMConfigurationError` when nothing is usable, which the app maps to HTTP 503.

Every test configures providers explicitly so the developer's local `.env` never leaks in.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from app.core.llm import LLM_CONFIG_REQUIRED_BANNER, LLMConfigurationError, LLMProviderRotator
from app.main import create_app

FAKE_GEMINI_KEY = "AIzaFakeGeminiKeyForUnitTests000"


def _isolated_rotator(**providers: dict) -> LLMProviderRotator:
    """Rotator with every real provider disabled and key-less, then `providers` applied on top."""
    rotator = LLMProviderRotator()
    for pid in ("qwen", "gemini", "openai"):
        cfg = rotator.providers[pid]
        cfg.enabled = False
        cfg.api_key = ""
    for pid, overrides in providers.items():
        cfg = rotator.providers[pid]
        cfg.enabled = True
        for key, value in overrides.items():
            setattr(cfg, key, value)
    return rotator


def test_no_usable_provider_raises_llm_configuration_error() -> None:
    rotator = _isolated_rotator()
    with pytest.raises(LLMConfigurationError) as excinfo:
        rotator.build_chat_model("builder", "builder")
    assert LLM_CONFIG_REQUIRED_BANNER in str(excinfo.value)

    # Enabled but placeholder key is still "not configured" — mock never counts as a real model.
    rotator = _isolated_rotator(gemini={"api_key": "your-gemini-api-key-here"})
    with pytest.raises(LLMConfigurationError):
        rotator.build_chat_model("lead", "lead")


def test_gemini_builds_native_langchain_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    rotator = _isolated_rotator(gemini={"api_key": FAKE_GEMINI_KEY, "model": "gemini-2.5-pro"})
    rotator.update_config({"strategy": "single", "active_single_provider": "gemini"})

    selection = rotator.build_chat_model("lead", "lead")

    assert isinstance(selection.model, ChatGoogleGenerativeAI)
    assert selection.model.model == "gemini-2.5-pro"
    assert selection.model.timeout == 90
    assert selection.model.max_retries == 2
    assert selection.label == "gemini:gemini-2.5-pro"
    assert selection.fallbacks == [] and selection.middleware() == []
    assert rotator.rotation_history[-1].selected_provider == "gemini:gemini-2.5-pro"


def test_gemini_flash_fallback_is_wired_as_model_fallback_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
    rotator = _isolated_rotator(gemini={"api_key": FAKE_GEMINI_KEY, "model": "gemini-2.5-pro"})
    rotator.update_config({"strategy": "single", "active_single_provider": "gemini"})

    selection = rotator.build_chat_model("lead", "lead")

    assert selection.fallback_labels == ["gemini:gemini-2.5-flash"]
    middleware = selection.middleware()
    assert len(middleware) == 1 and isinstance(middleware[0], ModelFallbackMiddleware)
    # The primary stays a real chat model so deepagents can still call `bind_tools`.
    assert hasattr(selection.model, "bind_tools")


def test_internal_qwen_with_empty_key_uses_chat_openai_and_role_chain() -> None:
    rotator = _isolated_rotator(
        qwen={"api_key": "EMPTY", "base_url": "http://qwen.internal:8001/v1", "model": "qwen3.8-flash"},
        gemini={"api_key": FAKE_GEMINI_KEY, "model": "gemini-2.5-flash"},
    )
    rotator.update_config({"strategy": "role_based", "role_mapping": {"builder": "qwen"}})

    selection = rotator.build_chat_model("builder", "builder")

    assert isinstance(selection.model, ChatOpenAI)
    assert selection.label == "qwen:qwen3.8-flash"
    assert str(selection.model.openai_api_base).startswith("http://qwen.internal:8001")
    # Remaining ready providers in the chain become failover models.
    assert selection.fallback_labels == ["gemini:gemini-2.5-flash"]


def test_role_primary_without_credentials_is_skipped_and_recorded() -> None:
    rotator = _isolated_rotator(
        qwen={"api_key": "internal-qwen-api-key"},  # placeholder → not usable
        gemini={"api_key": FAKE_GEMINI_KEY, "model": "gemini-2.5-flash"},
    )
    rotator.update_config({"strategy": "role_based", "role_mapping": {"builder": "qwen"}})

    selection = rotator.build_chat_model("builder", "builder")

    assert selection.label == "gemini:gemini-2.5-flash"
    entry = rotator.rotation_history[-1]
    assert entry.fallback_triggered is True
    assert "qwen skipped" in entry.fallback_reason


def test_llm_configuration_error_maps_to_http_503() -> None:
    app = create_app()

    @app.get("/__test__/needs-llm")
    def needs_llm() -> dict:
        _isolated_rotator().build_chat_model("builder", "builder")
        return {"ok": True}

    # No `with` block: the lifespan (demo workspace bootstrap) is not needed for this check.
    response = TestClient(app).get("/__test__/needs-llm")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "llm_config_required"
    assert body["error"] == LLM_CONFIG_REQUIRED_BANNER
    assert "GEMINI_API_KEY" in body["hint"]
