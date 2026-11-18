"""
abyss/orchestrator/deepagents_bridge.py
LangChain `deepagents` (v0.7+) Harness Integration for Abyss

- 왜 `claude-agent-sdk` 대신 `deepagents`를 선택했는가?
  `claude-agent-sdk`는 내부적으로 독점 Claude Code CLI 서브프로세스와 Anthropic 모델에
  강하게 결합되어 있어 사내 Qwen 3.8 Flash 엔드포인트를 직접 연결할 수 없습니다.
  반면 오픈소스(MIT) `deepagents`는 Claude Code의 핵심 하네스 기능(가상 파일시스템 도구,
  `TodoListMiddleware` 태스크 플래닝, `SKILL.md` 점진적 로딩, 서브에이전트 위임)을
  임의의 OpenAI 호환 엔드포인트(`ChatOpenAI`) 위에서 동일하게 구동할 수 있게 해줍니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from abyss.llm.provider_rotator import LLMProviderRotator, ProviderConfig


def create_worktree_deep_agent(
    rotator: LLMProviderRotator,
    agent_id: str,
    role: str,
    worktree_root: str | Path,
    extra_tools: list[Any] | None = None,
) -> dict[str, Any]:
    """
    선택된 프로바이더(Qwen 3.8 Flash / Gemini / OpenAI)를 기반으로
    LangChain `deepagents` 인스턴스를 생성하고 구성 메타데이터를 반환합니다.
    """
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
            ".agents/skills/codebase-onboarding/SKILL.md",
            ".agents/skills/socratic-onboarding/SKILL.md",
        ],
    }

    if chosen.is_mock or not chosen.has_valid_credentials():
        return {
            **config_summary,
            "instantiated": False,
            "note": "Running via Abyss deterministic AgentRuntime harness (set API key to instantiate live ChatOpenAI DeepAgent).",
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
                "Follow Claude Code worktree isolation rules and collaborate via the shared task board."
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
