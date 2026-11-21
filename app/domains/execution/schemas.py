"""
app/domains/execution/schemas.py
Pydantic DTOs for the Code Mode (`REPL`) Interactive Lab endpoints.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CodeModeRunRequest(BaseModel):
    code: str
    scenario_id: str = "custom"
    reset_sandbox: bool = True
    simulate_slow_hook: bool = False
    hook_on_failure: Literal["block", "continue"] = "block"


class CodeModeAgentRequest(BaseModel):
    instruction: str = Field(min_length=1, description="Natural-language coding instruction for the sandbox agent.")
    mode: Literal["direct", "repl"] = "repl"
    scenario_id: str | None = None
    reset_sandbox: bool = True
    completion_cmd: str | None = None
    rubric: str | None = None

    @field_validator("instruction")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            msg = "instruction must not be empty or whitespace."
            raise ValueError(msg)
        return cleaned


class CodeModeComparisonMetrics(BaseModel):
    direct_mode_llm_turns: int
    code_mode_llm_turns: int = 1
    inner_tool_calls_executed: int
    raw_intermediate_chars: int
    returned_to_model_chars: int
    context_saved_pct: int


class CodeModeRunResponse(BaseModel):
    result: dict[str, Any]
    comparison: CodeModeComparisonMetrics
