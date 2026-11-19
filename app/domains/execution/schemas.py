"""
app/domains/execution/schemas.py
Pydantic DTOs for the Code Mode (`REPL`) Interactive Lab endpoints.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class CodeModeRunRequest(BaseModel):
    code: str
    scenario_id: str = "custom"
    reset_sandbox: bool = True
    simulate_slow_hook: bool = False
    hook_on_failure: Literal["block", "continue"] = "block"


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
