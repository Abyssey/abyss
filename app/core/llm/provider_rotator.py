"""
app/core/llm/provider_rotator.py
Multi-Provider Live LLM Rotator & LangChain Chat-Model Factory (Qwen 3.8 Flash / Gemini / OpenAI).

- Manages internal Qwen 3.8 Flash endpoints (vLLM / SGLang / QwenCloud OpenAI-compatible API),
  Google Gemini (`ChatGoogleGenerativeAI`), and OpenAI (`ChatOpenAI`).
- Zero mock/offline simulation fallback: when no enabled provider has valid credentials,
  `build_chat_model` fails fast with `LLMConfigurationError` (mapped to HTTP 503
  `⛔ LLM CONFIG REQUIRED`).
"""

from __future__ import annotations

import itertools
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

load_dotenv()

LLM_CONFIG_REQUIRED_BANNER = "⛔ LLM CONFIG REQUIRED"
CHAT_MODEL_TIMEOUT_S = 90.0
CHAT_MODEL_MAX_RETRIES = 2


class LLMConfigurationError(RuntimeError):
    """활성화된 프로바이더 중 유효한 자격증명이 하나도 없을 때 발생합니다 (→ HTTP 503)."""

    http_status_code: int = 503


@dataclass
class ChatModelSelection:
    """`build_chat_model` 결과: 주 모델 + 장애 조치(Fallback) 모델 체인."""

    model: BaseChatModel
    label: str
    fallbacks: list[BaseChatModel] = field(default_factory=list)
    fallback_labels: list[str] = field(default_factory=list)

    def middleware(self) -> list[Any]:
        """deepagents/LangChain 에이전트용 `ModelFallbackMiddleware` (fallback이 없으면 빈 리스트)."""
        if not self.fallbacks:
            return []
        from langchain.agents.middleware import ModelFallbackMiddleware

        return [ModelFallbackMiddleware(self.fallbacks[0], *self.fallbacks[1:])]


class RotationStrategy(str, Enum):
    ROLE_BASED = "role_based"
    ROUND_ROBIN = "round_robin"
    FALLBACK = "fallback"
    SINGLE = "single"


@dataclass
class ProviderConfig:
    """개별 LLM 프로바이더 설정."""

    provider_id: str  # "qwen" | "gemini" | "openai"
    label: str
    base_url: str
    api_key: str
    model: str
    enabled: bool = True
    calls_count: int = 0
    errors_count: int = 0
    last_latency_ms: float = 0.0
    last_error: str = ""

    def has_valid_credentials(self) -> bool:
        cleaned = (self.api_key or "").strip()
        if not cleaned:
            return False
        if self.provider_id == "qwen" and cleaned == "EMPTY":
            return True
        placeholder_prefixes = ("your-", "internal-qwen-api-key", "sk-placeholder")
        return not any(cleaned.startswith(p) for p in placeholder_prefixes)

    def to_public_dict(self) -> dict[str, Any]:
        masked_key = ""
        if self.api_key and self.has_valid_credentials():
            if self.api_key == "EMPTY":
                masked_key = "EMPTY (unauthenticated internal)"
            elif len(self.api_key) > 8:
                masked_key = f"{self.api_key[:4]}...{self.api_key[-4:]}"
            else:
                masked_key = "***"
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "base_url": self.base_url,
            "api_key_masked": masked_key,
            "has_credentials": self.has_valid_credentials(),
            "model": self.model,
            "enabled": self.enabled,
            "calls_count": self.calls_count,
            "errors_count": self.errors_count,
            "last_latency_ms": round(self.last_latency_ms, 1),
            "last_error": self.last_error,
        }


@dataclass
class RotationLogEntry:
    """프로바이더 라우팅/로테이션 이력."""

    timestamp: float
    agent_id: str
    role: str
    strategy: str
    selected_provider: str
    model: str
    fallback_triggered: bool = False
    fallback_reason: str = ""
    latency_ms: float = 0.0


class LLMProviderRotator:
    """멀티 프로바이더 Live LLM 로테이션 및 Fallback 라우터."""

    def __init__(self) -> None:
        self.strategy: RotationStrategy = RotationStrategy(
            os.getenv("ROTATION_STRATEGY", "role_based")
        )
        raw_active = os.getenv("ACTIVE_PROVIDER", "gemini").strip()
        self.active_single_provider: str = raw_active if raw_active in ("qwen", "gemini", "openai") else "gemini"

        self.providers: dict[str, ProviderConfig] = {
            "qwen": ProviderConfig(
                provider_id="qwen",
                label="Qwen 3.8 Flash (Internal Endpoint)",
                base_url=os.getenv("QWEN_BASE_URL", "http://localhost:8001/v1"),
                api_key=os.getenv("QWEN_API_KEY", "internal-qwen-api-key"),
                model=os.getenv("QWEN_MODEL", "qwen3.8-flash"),
                enabled=os.getenv("QWEN_ENABLED", "true").lower() == "true",
            ),
            "gemini": ProviderConfig(
                provider_id="gemini",
                label="Google Gemini Flash",
                base_url=os.getenv(
                    "GEMINI_BASE_URL",
                    "https://generativelanguage.googleapis.com/v1beta/openai/",
                ),
                api_key=os.getenv("GEMINI_API_KEY", ""),
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                enabled=os.getenv("GEMINI_ENABLED", "true").lower() == "true",
            ),
            "openai": ProviderConfig(
                provider_id="openai",
                label="OpenAI GPT-4o-mini",
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key=os.getenv("OPENAI_API_KEY", ""),
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                enabled=os.getenv("OPENAI_ENABLED", "false").lower() == "true",
            ),
        }

        self.role_mapping: dict[str, str] = {
            "lead": "gemini",
            "builder": "qwen",
            "worker-analyzer": "qwen",
            "worker-tutor": "qwen",
            "worker-web": "gemini",
            "reviewer": "gemini",
        }

        self.fallback_order: list[str] = ["qwen", "gemini", "openai"]
        self._rr_counter = itertools.count()
        self.rotation_history: list[RotationLogEntry] = []

    def has_live_credentials(self) -> bool:
        return any(cfg.enabled and cfg.has_valid_credentials() for cfg in self.providers.values())

    def active_live_label(self) -> str | None:
        for cfg in self.select_candidate_chain("lead", "lead"):
            if cfg.enabled and cfg.has_valid_credentials():
                return f"{cfg.provider_id}:{cfg.model}"
        return None

    def get_state(self) -> dict[str, Any]:
        live_label = self.active_live_label()
        return {
            "strategy": self.strategy.value,
            "active_single_provider": self.active_single_provider,
            "has_live_credentials": live_label is not None,
            "active_live_label": live_label,
            "status_banner": f"🟢 LIVE LLM ({live_label})" if live_label else LLM_CONFIG_REQUIRED_BANNER,
            "role_mapping": dict(self.role_mapping),
            "fallback_order": list(self.fallback_order),
            "providers": {
                pid: cfg.to_public_dict() for pid, cfg in self.providers.items()
            },
            "recent_rotations": [
                {
                    "timestamp": e.timestamp,
                    "agent_id": e.agent_id,
                    "role": e.role,
                    "strategy": e.strategy,
                    "selected_provider": e.selected_provider,
                    "model": e.model,
                    "fallback_triggered": e.fallback_triggered,
                    "fallback_reason": e.fallback_reason,
                    "latency_ms": round(e.latency_ms, 1),
                }
                for e in self.rotation_history[-25:]
            ],
        }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """웹 UI에서 전달된 프로바이더/로테이션 설정을 즉시 반영합니다."""
        if "strategy" in payload:
            self.strategy = RotationStrategy(payload["strategy"])
        if "active_single_provider" in payload:
            candidate = str(payload["active_single_provider"])
            if candidate in self.providers:
                self.active_single_provider = candidate
        if "role_mapping" in payload and isinstance(payload["role_mapping"], dict):
            for role_key, pid in payload["role_mapping"].items():
                if pid in self.providers:
                    self.role_mapping[role_key] = pid
        if "providers" in payload and isinstance(payload["providers"], dict):
            for pid, pdata in payload["providers"].items():
                if pid not in self.providers or not isinstance(pdata, dict):
                    continue
                cfg = self.providers[pid]
                if "base_url" in pdata and pdata["base_url"]:
                    cfg.base_url = str(pdata["base_url"]).strip()
                if "api_key" in pdata and pdata["api_key"] and "..." not in str(pdata["api_key"]):
                    cfg.api_key = str(pdata["api_key"]).strip()
                if "model" in pdata and pdata["model"]:
                    cfg.model = str(pdata["model"]).strip()
                if "enabled" in pdata:
                    cfg.enabled = bool(pdata["enabled"])
        return self.get_state()

    def select_candidate_chain(self, agent_id: str, role: str) -> list[ProviderConfig]:
        """현재 로테이션 전략에 따라 시도할 프로바이더 우선순위 체인을 반환합니다."""
        enabled_list = [cfg for cfg in self.providers.values() if cfg.enabled]
        if not enabled_list:
            return []

        if self.strategy == RotationStrategy.SINGLE:
            primary = self.providers.get(self.active_single_provider) or enabled_list[0]
            rest = [
                self.providers[pid]
                for pid in self.fallback_order
                if pid != primary.provider_id and self.providers[pid].enabled
            ]
            return ([primary] if primary.enabled else []) + rest

        if self.strategy == RotationStrategy.ROLE_BASED:
            target_pid = self.role_mapping.get(agent_id) or self.role_mapping.get(role, "qwen")
            primary = self.providers.get(target_pid) or enabled_list[0]
            rest = [
                self.providers[pid]
                for pid in self.fallback_order
                if pid != primary.provider_id and self.providers[pid].enabled
            ]
            return ([primary] if primary.enabled else []) + rest

        if self.strategy == RotationStrategy.ROUND_ROBIN:
            ready = [cfg for cfg in enabled_list if cfg.has_valid_credentials()]
            pool = ready or enabled_list
            idx = next(self._rr_counter) % len(pool)
            return pool[idx:] + pool[:idx]

        # FALLBACK 전략
        return [
            self.providers[pid]
            for pid in self.fallback_order
            if pid in self.providers and self.providers[pid].enabled
        ]

    def build_chat_model(self, agent_id: str, role: str) -> ChatModelSelection:
        """
        현재 로테이션 전략으로 LangChain `BaseChatModel`을 만듭니다 (deepagents 엔진용).
        쓸 수 있는 프로바이더가 하나도 없으면 `LLMConfigurationError` (→ HTTP 503).
        """
        candidates = self.select_candidate_chain(agent_id=agent_id, role=role)
        ready: list[ProviderConfig] = []
        skipped: list[str] = []
        for cfg in candidates:
            if cfg in ready:
                continue
            if not cfg.enabled:
                skipped.append(f"{cfg.provider_id} disabled")
            elif not cfg.has_valid_credentials():
                skipped.append(f"{cfg.provider_id} skipped (API key not configured)")
            else:
                ready.append(cfg)

        if not ready:
            raise LLMConfigurationError(
                f"{LLM_CONFIG_REQUIRED_BANNER}: no enabled LLM provider with valid credentials "
                f"for agent '{agent_id}' (role '{role}'). "
                "Set GEMINI_API_KEY (or QWEN_*/OPENAI_*) in .env or configure a provider in the UI."
            )

        models: list[tuple[BaseChatModel, str]] = []
        for cfg in ready:
            models.append(
                (self._instantiate_chat_model(cfg, cfg.model), f"{cfg.provider_id}:{cfg.model}")
            )
            if cfg.provider_id == "gemini":
                flash_fallback = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
                if flash_fallback and flash_fallback != cfg.model:
                    models.append(
                        (
                            self._instantiate_chat_model(cfg, flash_fallback),
                            f"gemini:{flash_fallback}",
                        )
                    )

        primary_cfg = ready[0]
        primary_cfg.calls_count += 1
        self.rotation_history.append(
            RotationLogEntry(
                timestamp=time.time(),
                agent_id=agent_id,
                role=role,
                strategy=self.strategy.value,
                selected_provider=models[0][1],
                model=primary_cfg.model,
                fallback_triggered=bool(candidates and primary_cfg is not candidates[0]),
                fallback_reason="; ".join(skipped),
            )
        )
        return ChatModelSelection(
            model=models[0][0],
            label=models[0][1],
            fallbacks=[m for m, _ in models[1:]],
            fallback_labels=[label for _, label in models[1:]],
        )

    @staticmethod
    def _instantiate_chat_model(cfg: ProviderConfig, model_name: str) -> BaseChatModel:
        if cfg.provider_id == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=model_name,
                google_api_key=cfg.api_key,
                timeout=CHAT_MODEL_TIMEOUT_S,
                max_retries=CHAT_MODEL_MAX_RETRIES,
            )

        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": cfg.api_key,
            "base_url": cfg.base_url,
            "temperature": 0.2,
            "timeout": CHAT_MODEL_TIMEOUT_S,
            "max_retries": CHAT_MODEL_MAX_RETRIES,
        }
        if cfg.provider_id == "qwen":
            import httpx

            kwargs["http_client"] = httpx.Client(trust_env=False, timeout=CHAT_MODEL_TIMEOUT_S)
        return ChatOpenAI(**kwargs)

    def test_provider_connection(self, provider_id: str) -> dict[str, Any]:
        """웹 UI에서 특정 프로바이더(Qwen, Gemini, OpenAI) 연결 상태를 즉시 테스트합니다."""
        cfg = self.providers.get(provider_id)
        if not cfg:
            return {"ok": False, "message": f"Unknown provider '{provider_id}'"}
        if not cfg.has_valid_credentials():
            return {
                "ok": False,
                "provider_id": provider_id,
                "model": cfg.model,
                "message": f"API Key for '{cfg.label}' is not set yet. Enter a valid key in the UI.",
            }
        start_ts = time.perf_counter()
        try:
            chat_model = self._instantiate_chat_model(cfg, cfg.model)
            resp = chat_model.invoke("Reply with OK")
            elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
            cfg.last_latency_ms = elapsed_ms
            cfg.last_error = ""
            reply = str(getattr(resp, "content", "OK") or "OK").strip()
            return {
                "ok": True,
                "provider_id": provider_id,
                "model": cfg.model,
                "latency_ms": round(elapsed_ms, 1),
                "message": f"Connected successfully ({round(elapsed_ms, 1)}ms): '{reply[:60]}'",
            }
        except Exception as exc:  # noqa: BLE001
            cfg.errors_count += 1
            cfg.last_error = str(exc)[:200]
            return {
                "ok": False,
                "provider_id": provider_id,
                "model": cfg.model,
                "message": f"Connection failed: {cfg.last_error}",
            }
