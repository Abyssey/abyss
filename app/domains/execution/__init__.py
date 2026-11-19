"""
Focus Domain #2: Execution Engine & Claude Code `Code Mode (REPL)` Kick.

Core modules (`code_mode.py`, `runtime.py`) depend ONLY on Python stdlib and `app.domains.guardrails.*`.
"""

from app.domains.execution.code_mode import (
    CODE_MODE_TOOL_DESCRIPTION,
    REPL_TOOL_NAME,
    BudgetExceeded,
    CodeModeExecutor,
    CodeReview,
    CodeRunResult,
    CodeToolCall,
    CommandFailed,
    HookSpec,
    ShellResult,
    ToolRefused,
    compile_program,
)
from app.domains.execution.runtime import AgentRuntime, HandoffArtifact, ToolCallRecord

__all__ = [
    "AgentRuntime",
    "BudgetExceeded",
    "CODE_MODE_TOOL_DESCRIPTION",
    "CodeModeExecutor",
    "CodeReview",
    "CodeRunResult",
    "CodeToolCall",
    "CommandFailed",
    "HandoffArtifact",
    "HookSpec",
    "REPL_TOOL_NAME",
    "ShellResult",
    "ToolCallRecord",
    "ToolRefused",
    "compile_program",
]
