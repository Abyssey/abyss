"""
tests/helpers_llm_doubles.py
Deterministic LangChain `BaseChatModel` test double for exercising the full
`create_deep_agent` LangGraph compilation, tool execution, subagents, `RubricMiddleware`,
`PytestGateMiddleware`, and `ArtifactWorkflowMiddleware` without live network calls.

Strictly confined to `tests/` (never imported from `app/`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

StepSpec = AIMessage | Callable[[list[BaseMessage], list[str]], AIMessage]


def ai_tool_call(
    name: str,
    args: Mapping[str, Any],
    *,
    content: str = "",
    call_id: str | None = None,
) -> AIMessage:
    """Build an `AIMessage` requesting a single tool call."""
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "id": call_id or f"call_{uuid.uuid4().hex[:8]}",
                "name": name,
                "args": dict(args),
            }
        ],
        usage_metadata={"input_tokens": 25, "output_tokens": 15, "total_tokens": 40},
    )


def ai_text(content: str) -> AIMessage:
    """Build a plain final/intermediate `AIMessage` with token metadata."""
    return AIMessage(
        content=content,
        usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
    )


def grader_verdict(
    result: str = "satisfied",
    explanation: str = "All rubric criteria are satisfied.",
    criteria: Sequence[Mapping[str, Any]] | None = None,
) -> AIMessage:
    """Build a `GraderResponse` structured-output tool call for `RubricMiddleware`."""
    effective_criteria = (
        [dict(c) for c in criteria]
        if criteria is not None
        else [{"name": "Implementation satisfies task requirements", "passed": True}]
    )
    return ai_tool_call(
        "GraderResponse",
        {
            "result": result,
            "explanation": explanation,
            "criteria": effective_criteria,
        },
    )


@dataclass
class RecordedModelCall:
    messages: list[BaseMessage]
    bound_tools: list[str]


@dataclass
class _SharedScriptState:
    steps: list[StepSpec]
    grader_steps: list[StepSpec]
    step_idx: int = 0
    grader_idx: int = 0
    calls: list[RecordedModelCall] = field(default_factory=list)


def _tool_name(tool_obj: Any) -> str:
    if isinstance(tool_obj, str):
        return tool_obj
    if hasattr(tool_obj, "name") and isinstance(tool_obj.name, str):
        return tool_obj.name
    if isinstance(tool_obj, Mapping):
        if "name" in tool_obj:
            return str(tool_obj["name"])
        fn = tool_obj.get("function")
        if isinstance(fn, Mapping) and "name" in fn:
            return str(fn["name"])
    return type(tool_obj).__name__


class ScriptedDeepAgentChatModel(BaseChatModel):
    """
    `BaseChatModel` test double that supports `bind_tools` and replays a scripted
    sequence of `AIMessage`s across main agent turns, subagents, and `RubricMiddleware`.
    """

    model_name: str = "scripted-deep-agent"
    _shared: _SharedScriptState = PrivateAttr()
    _bound_tool_names: list[str] = PrivateAttr(default_factory=list)

    def __init__(
        self,
        steps: Sequence[StepSpec],
        *,
        grader_steps: Sequence[StepSpec] = (),
        model_name: str = "scripted-deep-agent",
        _shared: _SharedScriptState | None = None,
        _bound_tool_names: Sequence[str] = (),
    ) -> None:
        super().__init__(model_name=model_name)
        self._shared = _shared or _SharedScriptState(
            steps=list(steps),
            grader_steps=list(grader_steps),
        )
        self._bound_tool_names = list(_bound_tool_names)

    @property
    def _llm_type(self) -> str:
        return "scripted-deep-agent"

    @property
    def calls(self) -> list[RecordedModelCall]:
        return self._shared.calls

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: Any = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ScriptedDeepAgentChatModel:
        names = [_tool_name(t) for t in tools]
        return ScriptedDeepAgentChatModel(
            steps=(),
            model_name=self.model_name,
            _shared=self._shared,
            _bound_tool_names=names,
        )

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        self._shared.calls.append(
            RecordedModelCall(messages=list(messages), bound_tools=list(self._bound_tool_names))
        )
        # Route `RubricMiddleware` nested grader calls to `grader_steps` when bound to `GraderResponse`.
        if "GraderResponse" in self._bound_tool_names:
            # Note: after `ToolStrategy` parses `GraderResponse`, `create_agent` terminates immediately
            # unless an error occurred; if `grader_steps` has an entry, consume it.
            if self._shared.grader_idx < len(self._shared.grader_steps):
                spec = self._shared.grader_steps[self._shared.grader_idx]
                self._shared.grader_idx += 1
                return spec(messages, self._bound_tool_names) if callable(spec) else spec
            return grader_verdict("satisfied")

        if self._shared.step_idx >= len(self._shared.steps):
            raise AssertionError(
                f"ScriptedDeepAgentChatModel exhausted all {len(self._shared.steps)} steps! "
                f"Bound tools={self._bound_tool_names}, last message={messages[-1]!r}"
            )
        spec = self._shared.steps[self._shared.step_idx]
        self._shared.step_idx += 1
        return spec(messages, self._bound_tool_names) if callable(spec) else spec

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ChatResult:
        msg = self._next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])
