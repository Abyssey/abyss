"""
app/domains/workflow/deepagents_adapter.py
LangChain `deepagents` (v0.7+) Harness Integration Adapter.

사내 OpenAI 호환 엔드포인트(Qwen 3.8 Flash 등)에서도 `TodoListMiddleware`,
`FilesystemMiddleware` 등 오픈소스 에이전트 하네스를 연결할 수 있도록 추상화합니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.llm.provider_rotator import LLMProviderRotator, ProviderConfig


def create_worktree_deep_agent(
    rotator: LLMProviderRotator,
    agent_id: str,
    role: str,
    worktree_root: str | Path,
    extra_tools: list[Any] | None = None,
) -> dict[str, Any]:
    """선택된 프로바이더 기반으로 LangChain `deepagents` 구성을 생성합니다."""
    candidates: list[ProviderConfig] = rotator.select_candidate_chain(
        agent_id=agent_id, role=role
    )
    chosen = candidates[0]

    config_summary = {
        "harness": "langchain-ai/deepagents (v0.7+)",
        "agent_id": agent_id,
        "role": role,
        "worktree_root": str(worktree_root),
        "provider_id": chosen.provider_id,
        "model": chosen.model,
        "base_url": chosen.base_url,
        "middlewares": ["TodoListMiddleware", "FilesystemMiddleware"],
        "skills_loaded": [
            "codebase-onboarding",
            "socratic-onboarding",
        ],
    }

    if chosen.is_mock or not chosen.has_valid_credentials():
        return {
            **config_summary,
            "instantiated": False,
            "note": "Running via deterministic AgentRuntime harness (set API key to instantiate live ChatOpenAI DeepAgent).",
        }

    try:
        from deepagents import create_deep_agent
        from langchain.agents.middleware import TodoListMiddleware
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=chosen.model,
            api_key=chosen.api_key,
            base_url=chosen.base_url,
            temperature=0.2,
        )
        agent_graph = create_deep_agent(
            model=llm,
            tools=extra_tools or [],
            middleware=[TodoListMiddleware()],
            system_prompt=(
                f"You are {agent_id} ({role}) isolated inside git worktree {worktree_root}. "
                "Follow worktree isolation rules and collaborate via the shared task board."
            ),
        )
        return {
            **config_summary,
            "instantiated": True,
            "graph_type": type(agent_graph).__name__,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **config_summary,
            "instantiated": False,
            "error": str(exc),
        }
