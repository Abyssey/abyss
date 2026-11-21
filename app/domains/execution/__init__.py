"""
Focus Domain #2: Execution Engine & Claude Code `Code Mode (REPL)` Kick.

Core modules (`code_mode.py`, `runtime.py`) depend ONLY on Python stdlib and `app.domains.guardrails.*`.
"""

from app.domains.execution.artifact_workflow import (
    ArtifactWorkflowMiddleware,
    PlanReviewPolicy,
    WorkflowKind,
    default_plan_review_policy,
    render_task_markdown,
    stamp_validation_results,
)
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
from app.domains.execution.deepagent_backend import (
    DEFAULT_PERMISSIONS,
    BlockedAttempt,
    GuardedWorktreeBackend,
    ReadOnlyGuardedBackend,
)
from app.domains.execution.runtime import (
    AgentMode,
    DeepAgentResult,
    DeepAgentRuntime,
    PytestGateMiddleware,
    PytestGateRun,
    SubagentCallRecord,
    TurnTraceEntry,
    extract_thinking_and_content,
)

__all__ = [
    "AgentMode",
    "ArtifactWorkflowMiddleware",
    "BlockedAttempt",
    "BudgetExceeded",
    "CODE_MODE_TOOL_DESCRIPTION",
    "CodeModeExecutor",
    "CodeReview",
    "CodeRunResult",
    "CodeToolCall",
    "CommandFailed",
    "DEFAULT_PERMISSIONS",
    "DeepAgentResult",
    "DeepAgentRuntime",
    "GuardedWorktreeBackend",
    "HookSpec",
    "PlanReviewPolicy",
    "PytestGateMiddleware",
    "PytestGateRun",
    "REPL_TOOL_NAME",
    "ReadOnlyGuardedBackend",
    "ShellResult",
    "SubagentCallRecord",
    "ToolRefused",
    "TurnTraceEntry",
    "WorkflowKind",
    "compile_program",
    "default_plan_review_policy",
    "extract_thinking_and_content",
    "render_task_markdown",
    "stamp_validation_results",
]
