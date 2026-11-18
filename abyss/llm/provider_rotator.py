"""
abyss/llm/provider_rotator.py
Multi-Provider LLM Rotator (Qwen 3.8 Flash / Google Gemini / OpenAI / Mock)

- 사내 Qwen 3.8 Flash 엔드포인트(vLLM / SGLang / QwenCloud OpenAI 호환 API),
  Google Gemini API(OpenAI 호환 엔드포인트), OpenAI API, 그리고 오프라인/발표장용
  결정론적 Mock 프로바이더를 통합 관리합니다.
- 웹 관제 UI에서 코드 수정이나 서버 재시작 없이 실시간으로:
  1) 로테이션 전략 변경 (`role_based`, `round_robin`, `fallback`, `single`)
  2) 각 프로바이더의 `base_url`, `api_key`, `model`, `enabled` 수정
  3) 역할별(Lead / Worker / Reviewer) 배정 프로바이더 변경
  을 수행할 수 있습니다.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


class RotationStrategy(str, Enum):
    ROLE_BASED = "role_based"  # 역할별 모델 분리 (예: Lead/Reviewer=Gemini, Workers=Qwen 3.8 Flash)
    ROUND_ROBIN = "round_robin"  # 매 요청마다 활성 프로바이더를 순환 호출 (Rate Limit 분산)
    FALLBACK = "fallback"  # 주 프로바이더 호출 실패 시 다음 활성 프로바이더로 자동 전환
    SINGLE = "single"  # 단일 지정 프로바이더만 사용


@dataclass
class ProviderConfig:
    """개별 LLM 프로바이더 설정."""

    provider_id: str  # "qwen" | "gemini" | "openai" | "mock"
    label: str
    base_url: str
    api_key: str
    model: str
    enabled: bool = True
    is_mock: bool = False
    calls_count: int = 0
    errors_count: int = 0
    last_latency_ms: float = 0.0
    last_error: str = ""

    def has_valid_credentials(self) -> bool:
        if self.is_mock:
            return True
        if not self.api_key:
            return False
        placeholder_prefixes = ("your-", "internal-qwen-api-key", "sk-placeholder")
        return not any(self.api_key.startswith(p) for p in placeholder_prefixes)

    def to_public_dict(self) -> dict[str, Any]:
        masked_key = ""
        if self.api_key:
            if len(self.api_key) > 8 and not self.api_key.startswith("your-"):
                masked_key = f"{self.api_key[:4]}...{self.api_key[-4:]}"
            else:
                masked_key = self.api_key
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "base_url": self.base_url,
            "api_key_masked": masked_key,
            "has_credentials": self.has_valid_credentials(),
            "model": self.model,
            "enabled": self.enabled,
            "is_mock": self.is_mock,
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
    """
    멀티 프로바이더 LLM 로테이션 및 Fallback 라우터.
    """

    def __init__(self) -> None:
        self.strategy: RotationStrategy = RotationStrategy(
            os.getenv("ROTATION_STRATEGY", "role_based")
        )
        self.active_single_provider: str = os.getenv("ACTIVE_PROVIDER", "gemini")

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
                label="Google Gemini 2.5 Flash",
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
            "mock": ProviderConfig(
                provider_id="mock",
                label="Deterministic Simulation (Offline Ready)",
                base_url="mock://local-harness",
                api_key="mock-key",
                model="abyss-deterministic-simulator",
                enabled=True,
                is_mock=True,
            ),
        }

        # 역할별 기본 프로바이더 매핑 (Claude Code subagent effort/model tiering 반영)
        self.role_mapping: dict[str, str] = {
            "lead": "gemini",
            "worker-analyzer": "qwen",
            "worker-tutor": "qwen",
            "worker-web": "gemini",
            "reviewer": "gemini",
        }

        self.fallback_order: list[str] = ["qwen", "gemini", "openai", "mock"]
        self._rr_counter = itertools.count()
        self.rotation_history: list[RotationLogEntry] = []

    def get_state(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "active_single_provider": self.active_single_provider,
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
            self.active_single_provider = str(payload["active_single_provider"])
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
        """
        현재 로테이션 전략에 따라 시도할 프로바이더 우선순위 체인을 반환합니다.
        """
        enabled_list = [cfg for cfg in self.providers.values() if cfg.enabled]
        if not enabled_list:
            return [self.providers["mock"]]

        if self.strategy == RotationStrategy.SINGLE:
            primary = self.providers.get(self.active_single_provider, self.providers["mock"])
            rest = [
                self.providers[pid]
                for pid in self.fallback_order
                if pid != primary.provider_id and self.providers[pid].enabled
            ]
            return [primary, *rest]

        if self.strategy == RotationStrategy.ROLE_BASED:
            target_pid = self.role_mapping.get(agent_id) or self.role_mapping.get(role, "qwen")
            primary = self.providers.get(target_pid, self.providers["mock"])
            rest = [
                self.providers[pid]
                for pid in self.fallback_order
                if pid != primary.provider_id and self.providers[pid].enabled
            ]
            return [primary, *rest]

        if self.strategy == RotationStrategy.ROUND_ROBIN:
            # 실제 자격증명이 있거나 mock인 활성 프로바이더들 사이를 순환
            ready = [cfg for cfg in enabled_list if cfg.has_valid_credentials()]
            if not ready:
                ready = [self.providers["mock"]]
            idx = next(self._rr_counter) % len(ready)
            rotated = ready[idx:] + ready[:idx]
            if self.providers["mock"] not in rotated:
                rotated.append(self.providers["mock"])
            return rotated

        # FALLBACK 전략
        chain = [
            self.providers[pid]
            for pid in self.fallback_order
            if pid in self.providers and self.providers[pid].enabled
        ]
        if not chain:
            chain = [self.providers["mock"]]
        return chain

    def invoke_chat(
        self,
        agent_id: str,
        role: str,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None = None,
        mock_responder: Any | None = None,
    ) -> dict[str, Any]:
        """
        선택된 프로바이더 체인을 따라 OpenAI 호환 Chat Completions API를 호출합니다.
        실패 또는 자격증명 미설정 시 자동으로 체인의 다음 프로바이더로 Fallback합니다.
        """
        candidates = self.select_candidate_chain(agent_id=agent_id, role=role)
        fallback_triggered = False
        fallback_reasons: list[str] = []

        for idx, cfg in enumerate(candidates):
            start_ts = time.perf_counter()
            if idx > 0:
                fallback_triggered = True

            if cfg.is_mock:
                cfg.calls_count += 1
                if mock_responder is not None:
                    result = mock_responder(messages, tools_schema or [])
                else:
                    result = {"content": "Simulated response completed.", "tool_calls": []}
                elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
                cfg.last_latency_ms = elapsed_ms
                self.rotation_history.append(
                    RotationLogEntry(
                        timestamp=time.time(),
                        agent_id=agent_id,
                        role=role,
                        strategy=self.strategy.value,
                        selected_provider=f"{cfg.provider_id}:{cfg.model}",
                        model=cfg.model,
                        fallback_triggered=fallback_triggered,
                        fallback_reason="; ".join(fallback_reasons),
                        latency_ms=elapsed_ms,
                    )
                )
                return {
                    **result,
                    "provider": f"{cfg.provider_id}:{cfg.model}",
                    "fallback_triggered": fallback_triggered,
                }

            if not cfg.has_valid_credentials():
                reason = f"{cfg.provider_id} skipped (API key not configured)"
                fallback_reasons.append(reason)
                continue

            try:
                cfg.calls_count += 1
                client = OpenAI(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    timeout=20.0,
                    max_retries=1,
                )
                kwargs: dict[str, Any] = {
                    "model": cfg.model,
                    "messages": messages,
                    "temperature": 0.2,
                }
                if tools_schema:
                    kwargs["tools"] = tools_schema

                completion = client.chat.completions.create(**kwargs)
                choice = completion.choices[0].message
                content = choice.content or ""

                # Qwen 3.8 / vLLM reasoning_content 필드 지원
                reasoning_extra = getattr(choice, "reasoning_content", None)
                if reasoning_extra and "<think>" not in content:
                    content = f"<think>{reasoning_extra}</think>\n{content}"

                parsed_tool_calls: list[dict[str, Any]] = []
                if choice.tool_calls:
                    for tc in choice.tool_calls:
                        parsed_tool_calls.append(
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                        )

                elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
                cfg.last_latency_ms = elapsed_ms
                cfg.last_error = ""

                self.rotation_history.append(
                    RotationLogEntry(
                        timestamp=time.time(),
                        agent_id=agent_id,
                        role=role,
                        strategy=self.strategy.value,
                        selected_provider=f"{cfg.provider_id}:{cfg.model}",
                        model=cfg.model,
                        fallback_triggered=fallback_triggered,
                        fallback_reason="; ".join(fallback_reasons),
                        latency_ms=elapsed_ms,
                    )
                )
                return {
                    "content": content,
                    "tool_calls": parsed_tool_calls,
                    "provider": f"{cfg.provider_id}:{cfg.model}",
                    "fallback_triggered": fallback_triggered,
                }
            except Exception as exc:  # noqa: BLE001
                cfg.errors_count += 1
                cfg.last_error = str(exc)[:200]
                fallback_reasons.append(f"{cfg.provider_id} error: {cfg.last_error}")
                continue

        # 모든 외부 호출이 실패한 경우 안전하게 Mock으로 응답
        mock_cfg = self.providers["mock"]
        mock_cfg.calls_count += 1
        result = (
            mock_responder(messages, tools_schema or [])
            if mock_responder
            else {"content": "Fallback simulation completed.", "tool_calls": []}
        )
        return {
            **result,
            "provider": f"mock:{mock_cfg.model}",
            "fallback_triggered": True,
        }

    def test_provider_connection(self, provider_id: str) -> dict[str, Any]:
        """웹 UI에서 특정 프로바이더(Qwen, Gemini, OpenAI) 연결 상태를 즉시 테스트합니다."""
        cfg = self.providers.get(provider_id)
        if not cfg:
            return {"ok": False, "message": f"Unknown provider '{provider_id}'"}
        if cfg.is_mock:
            return {
                "ok": True,
                "provider_id": provider_id,
                "model": cfg.model,
                "latency_ms": 1.2,
                "message": "Deterministic Mock provider is ready for offline/live demo.",
            }
        if not cfg.has_valid_credentials():
            return {
                "ok": False,
                "provider_id": provider_id,
                "model": cfg.model,
                "message": f"API Key for '{cfg.label}' is not set yet. Enter a valid key in the UI.",
            }
        start_ts = time.perf_counter()
        try:
            client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=10.0, max_retries=0)
            resp = client.chat.completions.create(
                model=cfg.model,
                messages=[{"role": "user", "content": "Reply with OK"}],
                max_tokens=8,
            )
            elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
            cfg.last_latency_ms = elapsed_ms
            cfg.last_error = ""
            reply = (resp.choices[0].message.content or "OK").strip()
            return {
                "ok": True,
                "provider_id": provider_id,
                "model": cfg.model,
                "latency_ms": round(elapsed_ms, 1),
                "message": f"Connected successfully ({round(elapsed_ms, 1)}ms): '{reply}'",
            }
        except Exception as exc:  # noqa: BLE001
            cfg.last_error = str(exc)[:200]
            return {
                "ok": False,
                "provider_id": provider_id,
                "model": cfg.model,
                "message": f"Connection failed: {cfg.last_error}",
            }
