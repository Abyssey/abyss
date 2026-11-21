"""
================================================================================
Focus Domain #2 · app/domains/execution/runtime.py
`langchain-ai/deepagents` Unified Agent Execution Runtime (`DeepAgentRuntime`)
================================================================================

Wraps `create_deep_agent` with:
  1. 4-Check `GuardedWorktreeBackend` / `ReadOnlyGuardedBackend` isolation
  2. Antigravity 3-phase `ArtifactWorkflowMiddleware` (`implementation_plan.md` ->
     `task.md` -> stamped `walkthrough.md` + `request_review` LangGraph `interrupt()`)
  3. Dual completion gates: deterministic `PytestGateMiddleware` + LLM `RubricMiddleware`
  4. Read-only subagents (`repo-explorer`, `code-reviewer`) via `task` & Code Mode `run_python`
================================================================================
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deepagents import (
    FilesystemPermission,
    RubricMiddleware,
    SubAgent,
    create_deep_agent,
)
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.rubric import RubricEvaluation, RubricState
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    hook_config,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.domains.execution.artifact_workflow import (
    PLAN_FIRST_REFUSAL_PREFIX,
    ArtifactWorkflowMiddleware,
    ArtifactWorkspace,
    GateRuntimeTracker,
    PlanGateMiddleware,
    PlanReviewPolicy,
    WorkflowKind,
    default_plan_review_policy,
    is_artifacts_virtual_path,
    mount_artifacts_workspace,
    normalize_virtual_path,
)
from app.domains.execution.code_mode import (
    CODE_MODE_TOOL_DESCRIPTION,
    REPL_TOOL_NAME,
    CodeModeExecutor,
)
from app.domains.execution.deepagent_backend import (
    DEFAULT_PERMISSIONS,
    REFUSAL_PREFIX,
    BlockedAttempt,
)

AgentMode = Literal["builder", "reviewer", "worker", "direct", "repl"]
_UNSET: Any = object()
_THINK_REGEX = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def extract_thinking_and_content(raw_text: str | None) -> tuple[list[str], str]:
    """Separate `<think>...</think>` reasoning blocks from final assistant content."""
    if not raw_text:
        return [], ""
    thoughts = [m.strip() for m in _THINK_REGEX.findall(raw_text) if m.strip()]
    cleaned = _THINK_REGEX.sub("", raw_text).strip()
    return thoughts, cleaned


# ==============================================================================
# `langchain-ai/deepagents` Unified Engine (`DeepAgentRuntime`)
# ==============================================================================


class _RunPythonInput(BaseModel):
    code: str = Field(
        description="Async Python snippet using await sh/read/write/edit/ls/gather. Last expression is returned."
    )


def _default_rubric_enabled() -> bool:
    raw = os.getenv("AGENT_RUBRIC_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _backend_supports_execute(backend: BackendProtocol) -> bool:
    default_inner = getattr(backend, "default", None)
    target = default_inner if default_inner is not None else backend
    return callable(getattr(target, "execute", None))


@dataclass
class TurnTraceEntry:
    """Single `Turn N: Tool -> Observation` timeline entry for observability UI & audits."""

    turn: int
    tool_name: str
    arguments: dict[str, Any]
    observation: str
    status: Literal["ok", "error", "blocked"] = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "observation": self.observation[:1_200],
            "status": self.status,
        }


@dataclass
class SubagentCallRecord:
    """Recorded `task` delegation to an isolated subagent (`repo-explorer`, `code-reviewer`, ...)."""

    turn: int
    subagent_type: str
    description: str
    response_preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "subagent_type": self.subagent_type,
            "description": self.description,
            "response_preview": self.response_preview,
        }


@dataclass
class PytestGateRun:
    """Deterministic `completion_cmd` verification pass run by `PytestGateMiddleware`."""

    iteration: int
    command: str
    exit_code: int
    output: str
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output[:2_500],
            "passed": self.passed,
        }


@dataclass
class DeepAgentResult:
    """Structured result returned by `DeepAgentRuntime.run` / `DeepAgentRuntime.resume`."""

    status: Literal["completed", "awaiting_plan_review", "failed"]
    passed: bool
    summary: str
    mode: AgentMode
    workflow: WorkflowKind
    provider_used: str = ""
    thread_id: str = ""
    awaiting_plan_review: bool = False
    interrupt_payload: dict[str, Any] | None = None
    structured_response: Any = None
    llm_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    files_touched: list[str] = field(default_factory=list)
    todos: list[dict[str, str]] = field(default_factory=list)
    turn_trace: list[TurnTraceEntry] = field(default_factory=list)
    subagent_calls: list[SubagentCallRecord] = field(default_factory=list)
    blocked_attempts: list[BlockedAttempt] = field(default_factory=list)
    plan_refusals: list[dict[str, str]] = field(default_factory=list)
    pytest_gate_runs: list[PytestGateRun] = field(default_factory=list)
    rubric_status: str | None = None
    rubric_evaluations: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, str] = field(
        default_factory=lambda: {"implementation_plan": "", "task": "", "walkthrough": ""}
    )
    reasoning_trace: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": "langchain-ai/deepagents",
            "status": self.status,
            "passed": self.passed,
            "summary": self.summary,
            "mode": self.mode,
            "workflow": self.workflow,
            "provider_used": self.provider_used,
            "thread_id": self.thread_id,
            "awaiting_plan_review": self.awaiting_plan_review,
            "interrupt_payload": self.interrupt_payload,
            "llm_turns": self.llm_turns,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
            },
            "files_touched": list(self.files_touched),
            "todos": list(self.todos),
            "turn_trace": [t.to_dict() for t in self.turn_trace],
            "subagent_calls": [s.to_dict() for s in self.subagent_calls],
            "blocked_attempts_count": len(self.blocked_attempts),
            "blocked_attempts": [
                {
                    "operation": b.operation,
                    "target": b.target,
                    "check_name": b.check_name,
                    "reason": b.reason,
                    "remediation_hint": b.remediation_hint,
                }
                for b in self.blocked_attempts
            ],
            "plan_refusals": list(self.plan_refusals),
            "pytest_gate_runs": [g.to_dict() for g in self.pytest_gate_runs],
            "rubric_status": self.rubric_status,
            "rubric_evaluations": list(self.rubric_evaluations),
            "artifacts": dict(self.artifacts),
            "reasoning_trace": list(self.reasoning_trace),
        }


class PytestGateMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """
    Deterministic `after_agent` completion gate.

    Runs `completion_cmd` (default `pytest -q`) via the sandbox backend when the agent
    attempts to stop. On non-zero exit code, injects the traceback tail as a `HumanMessage`
    and jumps back to `"model"` (up to `max_gate_iterations` repair rounds). If retries are
    exhausted without passing, marks the run as failed and jumps directly to `"end"` so
    downstream LLM rubric/walkthrough steps are not wasted.
    """

    def __init__(
        self,
        *,
        backend: BackendProtocol,
        command: str,
        tracker: GateRuntimeTracker,
        max_gate_iterations: int = 3,
        tail_chars: int = 2_500,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.command = command
        self.tracker = tracker
        self.max_gate_iterations = max(0, int(max_gate_iterations))
        self.tail_chars = tail_chars
        self.event_callback = event_callback
        self._repairs_injected = 0

    @hook_config(can_jump_to=["model"])
    def after_agent(
        self,
        state: Mapping[str, Any],
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._run_gate(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(
        self,
        state: Mapping[str, Any],
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._run_gate(state)

    def _run_gate(self, state: Mapping[str, Any]) -> dict[str, Any] | None:
        # Never run pytest while the agent is still in PLANNING without an approved plan.
        if state.get("agent_phase") == "planning":
            return None

        # Skip redundant re-execution if pytest already passed and no workspace files changed
        # (for example, when the agent only wrote `/artifacts/walkthrough.md`).
        if (
            self.tracker.last_pytest_run is not None
            and self.tracker.last_pytest_run["passed"]
            and self.tracker.last_verified_mutation_seq == self.tracker.workspace_mutations
        ):
            return None

        exec_fn = getattr(self.backend, "execute", None)
        if not callable(exec_fn):
            return None

        resp = exec_fn(self.command)
        exit_code = int(resp.exit_code if resp.exit_code is not None else 1)
        output = str(resp.output or "")
        passed = exit_code == 0
        iteration = len(self.tracker.pytest_runs) + 1
        record = {
            "iteration": iteration,
            "command": self.command,
            "exit_code": exit_code,
            "output": output,
            "passed": passed,
        }
        self.tracker.pytest_runs.append(record)
        if self.event_callback is not None:
            self.event_callback(
                {
                    "event_type": "pytest_gate_evaluated",
                    "iteration": iteration,
                    "command": self.command,
                    "exit_code": exit_code,
                    "passed": passed,
                }
            )

        if passed:
            self.tracker.last_verified_mutation_seq = self.tracker.workspace_mutations
            self.tracker.pytest_gate_failed_terminally = False
            return None

        if self._repairs_injected >= self.max_gate_iterations:
            self.tracker.pytest_gate_failed_terminally = True
            return None

        self._repairs_injected += 1
        tail = output.strip()[-self.tail_chars :] or "(no output)"
        feedback = (
            f"[PYTEST GATE FAILED — repair {self._repairs_injected}/{self.max_gate_iterations}] "
            f"Command `{self.command}` exited with code {exit_code}.\n"
            f"Fix the failing code/tests inside the worktree before finishing:\n"
            f"```text\n{tail}\n```"
        )
        return {
            "messages": [HumanMessage(content=feedback, name="pytest_gate")],
            "jump_to": "model",
        }


class _GatedRubricMiddleware(RubricMiddleware):
    """
    `RubricMiddleware` wrapper that skips re-grading once a terminal verdict is reached
    unless new workspace mutations occurred afterwards (prevents duplicate grader calls
    when the walkthrough gate triggers a follow-up turn).
    """

    def __init__(self, *, tracker: GateRuntimeTracker, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._gate_tracker = tracker

    @hook_config(can_jump_to=["model"])
    def after_agent(
        self,
        state: RubricState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        if self._should_skip(state):
            return None
        if self._needs_fresh_regrade(state):
            state = RubricState(**state, _rubric_status=None, _rubric_iterations=0)
        res = super().after_agent(state, runtime)
        if res is not None and res.get("jump_to") != "model":
            self._gate_tracker.last_graded_mutation_seq = self._gate_tracker.workspace_mutations
        return res

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(
        self,
        state: RubricState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        if self._should_skip(state):
            return None
        if self._needs_fresh_regrade(state):
            state = RubricState(**state, _rubric_status=None, _rubric_iterations=0)
        res = await super().aafter_agent(state, runtime)
        if res is not None and res.get("jump_to") != "model":
            self._gate_tracker.last_graded_mutation_seq = self._gate_tracker.workspace_mutations
        return res

    def _should_skip(self, state: RubricState) -> bool:
        if self._gate_tracker.pytest_gate_failed_terminally:
            return True
        if state.get("agent_phase") == "planning":
            return True
        terminal = state.get("_rubric_status") in (
            "satisfied",
            "max_iterations_reached",
            "failed",
            "grader_error",
        )
        return bool(
            terminal
            and self._gate_tracker.last_graded_mutation_seq == self._gate_tracker.workspace_mutations
        )

    def _needs_fresh_regrade(self, state: RubricState) -> bool:
        terminal = state.get("_rubric_status") in ("satisfied", "max_iterations_reached")
        return bool(
            terminal
            and self._gate_tracker.last_graded_mutation_seq != self._gate_tracker.workspace_mutations
        )


class _ReadOnlySubagentMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """Enforces read-only exploration inside `repo-explorer` and `code-reviewer` subagents."""

    _FORBIDDEN = frozenset({"write_file", "edit_file", "delete", "execute", "run_python", "REPL"})

    def wrap_tool_call(
        self,
        request: Any,
        handler: Any,
    ) -> ToolMessage | Command[Any]:
        name = str(request.tool_call.get("name", ""))
        if name in self._FORBIDDEN:
            return ToolMessage(
                content=f"Error: subagent is read-only; tool '{name}' is not permitted.",
                name=name,
                tool_call_id=request.tool_call.get("id"),
                status="error",
            )
        return handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Any,
    ) -> ToolMessage | Command[Any]:
        name = str(request.tool_call.get("name", ""))
        if name in self._FORBIDDEN:
            return ToolMessage(
                content=f"Error: subagent is read-only; tool '{name}' is not permitted.",
                name=name,
                tool_call_id=request.tool_call.get("id"),
                status="error",
            )
        return await handler(request)


def _default_subagents(*, backend_has_exec: bool) -> list[SubAgent]:
    """Read-only explorer & independent reviewer subagents exposed through the `task` tool."""
    read_only_perms: list[FilesystemPermission] | None = (
        None
        if backend_has_exec
        else [
            *DEFAULT_PERMISSIONS,
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]
    )
    explorer: SubAgent = {
        "name": "repo-explorer",
        "description": (
            "Fast read-only codebase explorer. Delegate broad file discovery, symbol search, "
            "or architecture mapping here so your main context stays concise."
        ),
        "system_prompt": (
            "You are repo-explorer, a read-only codebase reconnaissance subagent. "
            "Use ls, glob, grep, and read_file to answer the delegated question concisely "
            "with exact file paths and line numbers. Never modify files."
        ),
        "middleware": [_ReadOnlySubagentMiddleware()],
    }
    reviewer: SubAgent = {
        "name": "code-reviewer",
        "description": (
            "Independent read-only code reviewer. Delegate pre-completion diff/file review "
            "for bugs, security leaks, and missing edge-case tests."
        ),
        "system_prompt": (
            "You are code-reviewer, an independent read-only auditor. Inspect the target files "
            "and report concrete bugs, security issues, or test gaps with file:line references."
        ),
        "middleware": [_ReadOnlySubagentMiddleware()],
    }
    if read_only_perms is not None:
        explorer["permissions"] = read_only_perms
        reviewer["permissions"] = read_only_perms
    return [explorer, reviewer]


class _HideTaskToolMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """Strips the `task` tool from model requests when `subagents=False`."""

    @staticmethod
    def _strip(request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        if not request.tools:
            return request
        filtered = [
            t
            for t in request.tools
            if (getattr(t, "name", None) or (t.get("name") if isinstance(t, Mapping) else None))
            != "task"
        ]
        return request.override(tools=filtered) if len(filtered) != len(request.tools) else request

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        return handler(self._strip(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Any,
    ) -> ModelResponse[ResponseT] | AIMessage:
        return await handler(self._strip(request))


class _ActiveRunSession:
    """Compiled graph + per-thread state kept alive when `request_review` pauses via `interrupt()`."""

    def __init__(
        self,
        *,
        graph: Any,
        artifacts_ws: ArtifactWorkspace,
        tracker: GateRuntimeTracker,
        rubric_evals: list[dict[str, Any]],
    ) -> None:
        self.graph = graph
        self.artifacts_ws = artifacts_ws
        self.tracker = tracker
        self.rubric_evals = rubric_evals


class DeepAgentRuntime:
    """
    Production coding-agent runtime backed by `langchain-ai/deepagents` (`create_deep_agent`).

    Supports:
      - Modes: `"builder" | "reviewer" | "worker" | "direct" | "repl"`
      - Antigravity 3-phase workflow (`workflow="planning"` default on builder/worker;
        `plan_review="request_review" | "always_proceed"`)
      - Dual completion gates: deterministic `PytestGateMiddleware` + LLM `RubricMiddleware`
      - Isolated read-only subagents (`repo-explorer`, `code-reviewer`) via `task`
      - Code Mode REPL batch execution (`run_python`) when `mode="repl"`
    """

    def __init__(
        self,
        *,
        model: BaseChatModel,
        backend: BackendProtocol,
        mode: AgentMode = "builder",
        workflow: WorkflowKind | None = None,
        plan_review: PlanReviewPolicy | None = None,
        artifacts_dir: str | Path | None = None,
        code_mode: CodeModeExecutor | None = None,
        completion_cmd: str | None = _UNSET,
        rubric: str | None = None,
        rubric_enabled: bool | None = None,
        max_gate_iterations: int = 3,
        subagents: bool = True,
        response_format: Any = None,
        extra_tools: Sequence[Any] = (),
        extra_middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
        permissions: list[FilesystemPermission] | None = None,
        checkpointer: Any = None,
        recursion_limit: int = 80,
        agent_id: str = "deep-agent",
        provider_label: str = "",
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.model = model
        self.workspace_backend = backend
        self.mode: AgentMode = mode
        self.workflow: WorkflowKind = (
            workflow if workflow is not None else ("planning" if mode in ("builder", "worker") else "fast")
        )
        self.plan_review: PlanReviewPolicy = (
            plan_review if plan_review is not None else default_plan_review_policy()
        )
        self.artifacts_dir = Path(artifacts_dir).resolve() if artifacts_dir is not None else None
        self.code_mode = code_mode
        if completion_cmd is _UNSET:
            self.completion_cmd: str | None = (
                "pytest -q"
                if mode in ("builder", "worker") and _backend_supports_execute(backend)
                else None
            )
        else:
            self.completion_cmd = completion_cmd
        self.rubric = rubric
        self.rubric_enabled = (
            rubric_enabled if rubric_enabled is not None else _default_rubric_enabled()
        )
        self.max_gate_iterations = max(0, int(max_gate_iterations))
        self.subagents_enabled = subagents
        self.response_format = response_format
        self.extra_tools = list(extra_tools)
        self.extra_middleware = list(extra_middleware)
        self.permissions = list(permissions) if permissions is not None else list(DEFAULT_PERMISSIONS)
        self.checkpointer = (
            checkpointer
            if checkpointer is not None
            else (InMemorySaver() if self.plan_review == "request_review" else None)
        )
        self.recursion_limit = recursion_limit
        self.agent_id = agent_id
        self.provider_label = provider_label
        self.event_callback = event_callback
        self._sessions: dict[str, _ActiveRunSession] = {}

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback({"agent_id": self.agent_id, "mode": self.mode, **payload})

    def _build_run_python_tool(self) -> BaseTool:
        if self.code_mode is None:
            msg = "DeepAgentRuntime(mode='repl') requires a `code_mode: CodeModeExecutor` instance."
            raise ValueError(msg)
        executor = self.code_mode

        def _sync_run_python(code: str) -> str:
            return executor.run(code).model_view()

        async def _async_run_python(code: str) -> str:
            res = await executor.arun(code)
            return res.model_view()

        return StructuredTool.from_function(
            name="run_python",
            description=CODE_MODE_TOOL_DESCRIPTION,
            func=_sync_run_python,
            coroutine=_async_run_python,
            args_schema=_RunPythonInput,
            infer_schema=False,
        )

    def _discover_memory_files(self, backend: BackendProtocol) -> list[str]:
        found: list[str] = []
        for candidate in ("/AGENTS.md", "/CLAUDE.md"):
            try:
                res = backend.read(candidate, offset=0, limit=50)
                if res.error is None and res.file_data is not None:
                    found.append(candidate)
            except Exception:  # noqa: BLE001
                continue
        return found

    def _build_session(self) -> _ActiveRunSession:
        artifacts_ws = mount_artifacts_workspace(self.workspace_backend, self.artifacts_dir)
        effective_backend = artifacts_ws.composite_backend
        tracker = GateRuntimeTracker()
        rubric_evals: list[dict[str, Any]] = []

        artifact_mw = ArtifactWorkflowMiddleware(
            workflow=self.workflow,
            plan_review=self.plan_review,
            artifacts=artifacts_ws,
            workspace_backend=self.workspace_backend,
            tracker=tracker,
            event_callback=self._emit,
        )
        plan_gate_mw = PlanGateMiddleware(workflow=self.workflow, artifacts=artifacts_ws)

        pytest_mw: PytestGateMiddleware | None = None
        if self.completion_cmd and _backend_supports_execute(effective_backend):
            pytest_mw = PytestGateMiddleware(
                backend=effective_backend,
                command=self.completion_cmd,
                tracker=tracker,
                max_gate_iterations=self.max_gate_iterations,
                event_callback=self._emit,
            )

        rubric_mw: _GatedRubricMiddleware | None = None
        if self.rubric_enabled and self.rubric:
            def _on_eval(ev: RubricEvaluation) -> None:
                rubric_evals.append(dict(ev))
                self._emit(
                    {
                        "event_type": "rubric_evaluated",
                        "iteration": ev.get("iteration", 0),
                        "result": ev.get("result"),
                        "explanation": ev.get("explanation", ""),
                    }
                )

            rubric_mw = _GatedRubricMiddleware(
                tracker=tracker,
                model=self.model,
                max_iterations=max(1, self.max_gate_iterations),
                on_evaluation=_on_eval,
            )

        # Forward order (`wrap_model_call` / `wrap_tool_call`):
        #   extra_middleware -> TodoListMiddleware -> ArtifactWorkflowMiddleware
        # Reverse order (`after_agent`):
        #   PlanGateMiddleware -> PytestGateMiddleware -> _GatedRubricMiddleware -> ArtifactWorkflowMiddleware
        middleware_stack: list[AgentMiddleware[Any, Any, Any]] = [
            *self.extra_middleware,
            TodoListMiddleware(),
            artifact_mw,
        ]
        if rubric_mw is not None:
            middleware_stack.append(rubric_mw)
        if pytest_mw is not None:
            middleware_stack.append(pytest_mw)
        middleware_stack.append(plan_gate_mw)
        if not self.subagents_enabled:
            middleware_stack.append(_HideTaskToolMiddleware())

        tools: list[Any] = list(self.extra_tools)
        if self.mode == "repl":
            tools.insert(0, self._build_run_python_tool())

        has_exec = _backend_supports_execute(effective_backend)
        memory_sources = self._discover_memory_files(self.workspace_backend)
        subagent_specs = _default_subagents(backend_has_exec=has_exec) if self.subagents_enabled else None

        # `deepagents 0.7.x` `FilesystemMiddleware` raises `NotImplementedError` if
        # `FilesystemPermission` rules are passed alongside a `SandboxBackendProtocol` backend
        # (`execute` enabled). For sandbox backends, `GuardedWorktreeBackend` + pre-flight
        # `ArtifactWorkflowMiddleware` enforce both file and shell permissions directly; for
        # non-sandbox backends (`ReadOnlyGuardedBackend`, `StateBackend`), `DEFAULT_PERMISSIONS`
        # are also wired into `FilesystemMiddleware`.
        effective_permissions: list[FilesystemPermission] | None
        if has_exec:
            effective_permissions = None
        else:
            effective_permissions = list(self.permissions)
            if self.mode == "reviewer":
                effective_permissions = [
                    *effective_permissions,
                    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
                ]

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "tools": tools or None,
            "middleware": middleware_stack,
            "subagents": subagent_specs,
            "memory": memory_sources or None,
            "permissions": effective_permissions,
            "backend": effective_backend,
            "response_format": self.response_format,
            "checkpointer": self.checkpointer,
            "name": self.agent_id,
        }
        graph = create_deep_agent(**create_kwargs)
        return _ActiveRunSession(
            graph=graph,
            artifacts_ws=artifacts_ws,
            tracker=tracker,
            rubric_evals=rubric_evals,
        )

    def _compose_system_prompt(self, caller_prompt: str) -> str:
        parts = [caller_prompt.strip()] if caller_prompt.strip() else []
        if self.mode == "repl":
            parts.append(
                "Code Mode (`run_python`) is enabled: batch multi-step file searches, reads, edits, "
                "and shell checks into a single `run_python` snippet whenever practical and return a concise dict."
            )
        elif self.mode == "reviewer":
            parts.append(
                "You are running in read-only Independent Reviewer mode. Inspect files and diffs without modifying any files."
            )
        return "\n\n".join(parts)

    def run(
        self,
        system_prompt: str,
        task_prompt: str,
        *,
        thread_id: str | None = None,
    ) -> DeepAgentResult:
        """Execute the `create_deep_agent` graph synchronously and return a `DeepAgentResult`."""
        tid = thread_id or f"run-{uuid.uuid4().hex[:12]}"
        session = self._build_session()
        full_system = self._compose_system_prompt(system_prompt)

        initial_messages: list[Any] = []
        if full_system:
            initial_messages.append(("system", full_system))
        initial_messages.append(("user", task_prompt))

        inputs: dict[str, Any] = {"messages": initial_messages}
        if self.rubric_enabled and self.rubric:
            inputs["rubric"] = self.rubric

        config: dict[str, Any] = {"recursion_limit": self.recursion_limit}
        if self.checkpointer is not None:
            config["configurable"] = {"thread_id": tid}

        state = session.graph.invoke(inputs, config)
        result = self._parse_state(state, session=session, thread_id=tid)
        if result.awaiting_plan_review:
            self._sessions[tid] = session
        else:
            session.artifacts_ws.cleanup_if_temp()
        return result

    def resume(
        self,
        *,
        thread_id: str,
        comments: str | Sequence[str] | None = None,
        proceed: bool = True,
    ) -> DeepAgentResult:
        """Resume an `awaiting_plan_review` thread after user Proceed / comment feedback."""
        if self.checkpointer is None:
            msg = "Cannot resume: DeepAgentRuntime has no checkpointer configured."
            raise RuntimeError(msg)
        session = self._sessions.get(thread_id)
        if session is None:
            session = self._build_session()
            self._sessions[thread_id] = session

        payload: dict[str, Any] = {"proceed": proceed}
        if comments:
            payload["comments"] = (
                [comments] if isinstance(comments, str) else [str(c) for c in comments]
            )

        config: dict[str, Any] = {
            "recursion_limit": self.recursion_limit,
            "configurable": {"thread_id": thread_id},
        }
        state = session.graph.invoke(Command(resume=payload), config)
        result = self._parse_state(state, session=session, thread_id=thread_id)
        if not result.awaiting_plan_review:
            self._sessions.pop(thread_id, None)
            session.artifacts_ws.cleanup_if_temp()
        return result

    def _parse_state(
        self,
        state: Mapping[str, Any],
        *,
        session: _ActiveRunSession,
        thread_id: str,
    ) -> DeepAgentResult:
        messages: Sequence[AnyMessage] = state.get("messages") or []
        tool_by_id: dict[str, ToolMessage] = {}
        for msg in messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                tool_by_id[msg.tool_call_id] = msg

        llm_turns = 0
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        turn_trace: list[TurnTraceEntry] = []
        subagent_calls: list[SubagentCallRecord] = []
        files_touched: list[str] = []
        reasoning_trace: list[str] = []
        last_ai_text = ""

        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            llm_turns += 1
            usage = getattr(msg, "usage_metadata", None) or {}
            if isinstance(usage, Mapping):
                input_tokens += int(usage.get("input_tokens", 0) or 0)
                output_tokens += int(usage.get("output_tokens", 0) or 0)
                total_tokens += int(usage.get("total_tokens", 0) or 0)

            raw_text = msg.text if hasattr(msg, "text") and isinstance(msg.text, str) else str(msg.content or "")
            thoughts, cleaned = extract_thinking_and_content(raw_text)
            reasoning_trace.extend(thoughts)
            if cleaned:
                last_ai_text = cleaned

            for tc in msg.tool_calls or []:
                tc_id = str(tc.get("id", ""))
                tc_name = str(tc.get("name", ""))
                tc_args = dict(tc.get("args") or {})
                tool_msg = tool_by_id.get(tc_id)
                obs = str(tool_msg.content) if tool_msg is not None else ""

                if obs.startswith(REFUSAL_PREFIX) or obs.startswith(PLAN_FIRST_REFUSAL_PREFIX):
                    entry_status: Literal["ok", "error", "blocked"] = "blocked"
                elif tool_msg is not None and getattr(tool_msg, "status", None) == "error":
                    entry_status = "error"
                else:
                    entry_status = "ok"

                turn_trace.append(
                    TurnTraceEntry(
                        turn=llm_turns,
                        tool_name=tc_name,
                        arguments=tc_args,
                        observation=obs,
                        status=entry_status,
                    )
                )

                if tc_name == "task":
                    subagent_calls.append(
                        SubagentCallRecord(
                            turn=llm_turns,
                            subagent_type=str(tc_args.get("subagent_type", "general-purpose")),
                            description=str(tc_args.get("description", "")),
                            response_preview=obs[:600],
                        )
                    )

                if entry_status == "ok" and tc_name in ("write_file", "edit_file", "delete"):
                    raw_target = str(tc_args.get("file_path") or tc_args.get("path") or "")
                    if raw_target and not is_artifacts_virtual_path(raw_target):
                        rel = normalize_virtual_path(raw_target).lstrip("/")
                        if rel and rel not in files_touched:
                            files_touched.append(rel)

                if entry_status == "ok" and tc_name in ("run_python", REPL_TOOL_NAME) and self.code_mode is not None:
                    last_cm = getattr(self.code_mode, "last_result", None)
                    for touched in getattr(last_cm, "files_touched", []) or []:
                        if touched not in files_touched:
                            files_touched.append(str(touched))

        raw_todos = state.get("todos") or []
        todos = [
            {"content": str(t.get("content", "")), "status": str(t.get("status", "pending"))}
            for t in raw_todos
            if isinstance(t, Mapping)
        ]

        artifacts = session.artifacts_ws.snapshot()
        blocked = list(getattr(self.workspace_backend, "blocked_attempts", []) or [])
        pytest_runs = [PytestGateRun(**r) for r in session.tracker.pytest_runs]
        rubric_evals = list(session.rubric_evals)
        rubric_status = str(rubric_evals[-1]["result"]) if rubric_evals else None
        structured = state.get("structured_response")

        interrupts = state.get("__interrupt__")
        if interrupts:
            first_int = interrupts[0]
            int_val = getattr(first_int, "value", first_int)
            payload = dict(int_val) if isinstance(int_val, Mapping) else {"value": int_val}
            return DeepAgentResult(
                status="awaiting_plan_review",
                passed=False,
                summary="Implementation plan ready — awaiting user review.",
                mode=self.mode,
                workflow=self.workflow,
                provider_used=self.provider_label,
                thread_id=thread_id,
                awaiting_plan_review=True,
                interrupt_payload=payload,
                structured_response=structured,
                llm_turns=llm_turns,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                files_touched=files_touched,
                todos=todos,
                turn_trace=turn_trace,
                subagent_calls=subagent_calls,
                blocked_attempts=blocked,
                plan_refusals=list(session.tracker.plan_refusals),
                pytest_gate_runs=pytest_runs,
                rubric_status=rubric_status,
                rubric_evaluations=rubric_evals,
                artifacts=artifacts,
                reasoning_trace=reasoning_trace,
            )

        passed = True
        if self.completion_cmd and _backend_supports_execute(session.artifacts_ws.composite_backend):
            if session.tracker.pytest_gate_failed_terminally:
                passed = False
            elif not pytest_runs or not pytest_runs[-1].passed:
                passed = False
        if self.workflow == "planning" and not artifacts["implementation_plan"].strip():
            passed = False
        if self.rubric_enabled and self.rubric and rubric_status in (
            "failed",
            "max_iterations_reached",
            "grader_error",
        ):
            passed = False

        status: Literal["completed", "awaiting_plan_review", "failed"] = (
            "completed" if passed else "failed"
        )
        summary = last_ai_text or ("Task completed." if passed else "Task failed completion gates.")
        return DeepAgentResult(
            status=status,
            passed=passed,
            summary=summary,
            mode=self.mode,
            workflow=self.workflow,
            provider_used=self.provider_label,
            thread_id=thread_id,
            awaiting_plan_review=False,
            interrupt_payload=None,
            structured_response=structured,
            llm_turns=llm_turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            files_touched=files_touched,
            todos=todos,
            turn_trace=turn_trace,
            subagent_calls=subagent_calls,
            blocked_attempts=blocked,
            plan_refusals=list(session.tracker.plan_refusals),
            pytest_gate_runs=pytest_runs,
            rubric_status=rubric_status,
            rubric_evaluations=rubric_evals,
            artifacts=artifacts,
            reasoning_trace=reasoning_trace,
        )
