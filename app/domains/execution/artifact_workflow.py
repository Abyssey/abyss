"""
app/domains/execution/artifact_workflow.py
Antigravity-inspired 3-phase coding-agent workflow (`PLANNING -> EXECUTION -> VERIFICATION`)
implemented on top of `langchain-ai/deepagents` + LangGraph middleware.

Produces three human-readable Markdown artifacts routed outside the git worktree via
`CompositeBackend` (`/artifacts/*` -> `data/tasks/<id>/artifacts/` or a managed scratch dir)
so artifacts never dirty `git status` or get committed into user branches:

  1. `/artifacts/implementation_plan.md` — written by the agent at the end of PLANNING
  2. `/artifacts/task.md`                — deterministically rendered from `write_todos`
  3. `/artifacts/walkthrough.md`         — written after verification, stamped with the
                                           actual deterministic `pytest` gate output
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import BackendProtocol
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from typing_extensions import NotRequired

WorkflowKind = Literal["planning", "fast"]
PlanReviewPolicy = Literal["always_proceed", "request_review"]
AgentPhase = Literal["planning", "execution", "completed"]

ARTIFACTS_ROUTE = "/artifacts/"
PLAN_VIRTUAL_PATH = "/artifacts/implementation_plan.md"
TASK_VIRTUAL_PATH = "/artifacts/task.md"
WALKTHROUGH_VIRTUAL_PATH = "/artifacts/walkthrough.md"

PLAN_BACKEND_KEY = "/implementation_plan.md"
TASK_BACKEND_KEY = "/task.md"
WALKTHROUGH_BACKEND_KEY = "/walkthrough.md"

PLAN_FIRST_REFUSAL_PREFIX = "[PLAN-FIRST REFUSAL]"

_VERIFIED_GATE_START = "<!-- abyss:verified-gate:start -->"
_VERIFIED_GATE_END = "<!-- abyss:verified-gate:end -->"
_VERIFIED_BLOCK_RE = re.compile(
    rf"{re.escape(_VERIFIED_GATE_START)}.*?{re.escape(_VERIFIED_GATE_END)}",
    re.DOTALL,
)

_FS_MUTATION_TOOLS = frozenset({"write_file", "edit_file", "delete"})
_EXEC_TOOLS = frozenset({"execute", "run_python", "REPL"})
_FS_TOOL_OP_MAP: dict[str, str] = {
    "ls": "ls",
    "read_file": "read",
    "glob": "glob",
    "grep": "grep",
    "write_file": "write",
    "edit_file": "edit",
    "delete": "delete",
}

_PLANNING_PHASE_PROMPT = """\
## Active Workflow Phase: PLANNING (Read-Only Exploration -> Implementation Plan)

You are in the **PLANNING** phase. Follow this strict sequence:
1. Inspect the codebase in read-only mode (`ls`, `read_file`, `glob`, `grep`, or `write_todos`).
   Do **NOT** modify workspace files or run `execute` yet — workspace edits and shell commands
   will be rejected with `[PLAN-FIRST REFUSAL]` until your plan is written.
2. Write `/artifacts/implementation_plan.md` using `write_file` with these Markdown sections:
   - `# Implementation Plan`
   - `## Goal`
   - `## User Review Required` (breaking changes, risks, or `None`)
   - `## Proposed Changes` (`[NEW]` / `[MODIFY]` / `[DELETE]` per file)
   - `## Verification Plan` (exact test commands and checks)"""

_EXECUTION_PHASE_PROMPT = """\
## Active Workflow Phase: EXECUTION & WALKTHROUGH

Your implementation plan is approved. Follow this sequence:
1. Track multi-step progress with `write_todos` (automatically mirrored to `/artifacts/task.md`).
2. Implement the planned edits cleanly inside the worktree and run tests via `execute` when available.
3. Before finishing, write `/artifacts/walkthrough.md` using `write_file` with these sections:
   - `# Walkthrough`
   - `## Changes Made`
   - `## Validation Results`"""


def default_plan_review_policy() -> PlanReviewPolicy:
    """Resolve `AGENT_PLAN_REVIEW` from the environment (`request_review` by default)."""
    raw = os.getenv("AGENT_PLAN_REVIEW", "request_review").strip().lower()
    if raw == "always_proceed":
        return "always_proceed"
    return "request_review"


def normalize_virtual_path(raw_path: str | None) -> str:
    """Normalize a virtual path while preserving `..` segments when they escape `/`."""
    if not raw_path:
        return "/"
    cleaned = raw_path.replace("\\", "/").strip()
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    # Check whether relative normalization before anchoring would escape root.
    rel_norm = posixpath.normpath(cleaned.lstrip("/"))
    if rel_norm == ".." or rel_norm.startswith("../"):
        return f"/{rel_norm}"
    norm = posixpath.normpath(cleaned)
    return "/" if norm == "." else norm


def is_artifacts_virtual_path(raw_path: str | None) -> bool:
    """True only when `raw_path` safely resolves inside `/artifacts/` without traversal."""
    if not raw_path or "\x00" in raw_path:
        return False
    norm = normalize_virtual_path(raw_path)
    return norm == "/artifacts" or norm.startswith(ARTIFACTS_ROUTE)


def render_task_markdown(todos: Sequence[Mapping[str, Any]]) -> str:
    """Deterministically render `TodoListMiddleware` items into `/artifacts/task.md`."""
    lines = ["# Task Checklist", ""]
    if not todos:
        lines.append("- [ ] (no tasks recorded)")
        return "\n".join(lines) + "\n"

    status_box = {
        "completed": "[x]",
        "in_progress": "[/]",
        "pending": "[ ]",
    }
    for item in todos:
        box = status_box.get(str(item.get("status", "pending")), "[ ]")
        content = str(item.get("content", "")).strip() or "(untitled task)"
        lines.append(f"- {box} {content}")
    return "\n".join(lines) + "\n"


def has_required_walkthrough_sections(markdown: str) -> bool:
    """Check that `walkthrough.md` has substantive Changes Made & Validation Results sections."""
    lowered = markdown.lower()
    if len(markdown.strip()) < 30:
        return False
    has_changes = any(h in lowered for h in ("changes made", "변경 사항", "변경사항", "## summary"))
    has_validation = any(
        h in lowered for h in ("validation results", "검증 결과", "검증결과", "## verification")
    )
    return has_changes and has_validation


def stamp_validation_results(
    walkthrough_md: str,
    *,
    command: str,
    exit_code: int,
    output: str,
    max_output_chars: int = 2_500,
) -> str:
    """Append or replace the deterministic test-gate output block in `walkthrough.md`."""
    trimmed = (output or "").strip()
    if len(trimmed) > max_output_chars:
        trimmed = f"... [truncated] ...\n{trimmed[-max_output_chars:]}"
    if not trimmed:
        trimmed = "(no stdout/stderr)"

    stamped_block = "\n".join(
        [
            _VERIFIED_GATE_START,
            "### Verified Command Output (Deterministic Gate)",
            f"- Command: `{command}`",
            f"- Exit Code: `{exit_code}`",
            "```text",
            trimmed,
            "```",
            _VERIFIED_GATE_END,
        ]
    )

    base = walkthrough_md.strip()
    if not base:
        base = "# Walkthrough\n\n## Changes Made\n- Completed requested changes.\n\n## Validation Results"

    if _VERIFIED_BLOCK_RE.search(base):
        return _VERIFIED_BLOCK_RE.sub(lambda _m: stamped_block, base) + "\n"

    if "validation results" not in base.lower() and "검증 결과" not in base.lower():
        base = f"{base}\n\n## Validation Results"
    return f"{base}\n\n{stamped_block}\n"


def _append_system_text(existing: SystemMessage | None, extra_text: str) -> SystemMessage:
    if existing is None:
        return SystemMessage(content=extra_text)
    blocks = list(existing.content_blocks)
    blocks.append({"type": "text", "text": f"\n\n{extra_text}"})
    return SystemMessage(content_blocks=blocks)


def _parse_plan_review_resume(resume_value: Any) -> tuple[bool, list[str]]:
    """Normalize `Command(resume=...)` payload into `(proceed, comments)`."""
    if resume_value is None or resume_value is True:
        return True, []
    if resume_value is False:
        return False, []
    if isinstance(resume_value, str):
        text = resume_value.strip()
        return True, [text] if text else []
    if isinstance(resume_value, Mapping):
        action = str(resume_value.get("action", "")).strip().lower()
        if "proceed" in resume_value:
            proceed = bool(resume_value["proceed"])
        elif "approved" in resume_value:
            proceed = bool(resume_value["approved"])
        elif action in ("revise", "reject", "request_changes"):
            proceed = False
        else:
            proceed = True

        raw_comments = resume_value.get("comments")
        comments: list[str] = []
        if isinstance(raw_comments, str) and raw_comments.strip():
            comments.append(raw_comments.strip())
        elif isinstance(raw_comments, Sequence) and not isinstance(raw_comments, (str, bytes)):
            comments.extend(str(c).strip() for c in raw_comments if str(c).strip())
        return proceed, comments
    return True, [str(resume_value).strip()]


@dataclass
class ArtifactWorkspace:
    """Holds the `/artifacts/` backend and directory for a `DeepAgentRuntime`."""

    composite_backend: BackendProtocol
    artifacts_backend: FilesystemBackend
    artifacts_dir: Path
    owns_temp_dir: bool = False

    def read_text(self, backend_key: str) -> str:
        res = self.artifacts_backend.read(backend_key, offset=0, limit=10_000)
        if res.error or res.file_data is None:
            return ""
        # `FilesystemBackend.read` returns plain content in `file_data["content"]`
        return str(res.file_data.get("content", ""))

    def write_text(self, backend_key: str, content: str) -> None:
        self.artifacts_backend.write(backend_key, content)

    def snapshot(self) -> dict[str, str]:
        return {
            "implementation_plan": self.read_text(PLAN_BACKEND_KEY),
            "task": self.read_text(TASK_BACKEND_KEY),
            "walkthrough": self.read_text(WALKTHROUGH_BACKEND_KEY),
        }

    def cleanup_if_temp(self) -> None:
        if self.owns_temp_dir and self.artifacts_dir.exists():
            shutil.rmtree(self.artifacts_dir, ignore_errors=True)


def mount_artifacts_workspace(
    backend: BackendProtocol,
    artifacts_dir: str | Path | None = None,
) -> ArtifactWorkspace:
    """Route `/artifacts/` to an isolated `FilesystemBackend` outside the worktree."""
    if artifacts_dir is not None:
        resolved_dir = Path(artifacts_dir).resolve()
        resolved_dir.mkdir(parents=True, exist_ok=True)
        owns_temp = False
    else:
        resolved_dir = Path(tempfile.mkdtemp(prefix="abyss_artifacts_")).resolve()
        owns_temp = True

    artifacts_fs = FilesystemBackend(root_dir=resolved_dir, virtual_mode=True)
    if isinstance(backend, CompositeBackend):
        routes = {**backend.routes, ARTIFACTS_ROUTE: artifacts_fs}
        composite: BackendProtocol = CompositeBackend(
            default=backend.default,
            routes=routes,
            artifacts_root=backend.artifacts_root,
        )
    else:
        composite = CompositeBackend(default=backend, routes={ARTIFACTS_ROUTE: artifacts_fs})
    return ArtifactWorkspace(
        composite_backend=composite,
        artifacts_backend=artifacts_fs,
        artifacts_dir=resolved_dir,
        owns_temp_dir=owns_temp,
    )


@dataclass
class GateRuntimeTracker:
    """Shared in-memory tracker across the workflow & verification middlewares for one run."""

    workspace_mutations: int = 0
    plan_refusals: list[dict[str, str]] = field(default_factory=list)
    pytest_runs: list[dict[str, Any]] = field(default_factory=list)
    last_verified_mutation_seq: int = -1
    last_graded_mutation_seq: int = -1
    pytest_gate_failed_terminally: bool = False

    @property
    def last_pytest_run(self) -> dict[str, Any] | None:
        return self.pytest_runs[-1] if self.pytest_runs else None


class ArtifactWorkflowState(AgentState[Any]):
    """Graph state extensions persisted across checkpoints (needed for `interrupt()` resume)."""

    agent_phase: NotRequired[AgentPhase]
    plan_approved: NotRequired[bool]
    plan_review_comments: NotRequired[list[str]]
    plan_reminders: NotRequired[int]
    walkthrough_attempts: NotRequired[int]


class PlanGateMiddleware(AgentMiddleware[ArtifactWorkflowState, ContextT, ResponseT]):
    """Inner-most `after_agent` check: ensures PLANNING finishes with `implementation_plan.md`."""

    state_schema = ArtifactWorkflowState

    def __init__(
        self,
        *,
        workflow: WorkflowKind,
        artifacts: ArtifactWorkspace,
        max_reminders: int = 2,
    ) -> None:
        super().__init__()
        self.workflow = workflow
        self.artifacts = artifacts
        self.max_reminders = max_reminders

    @hook_config(can_jump_to=["model"])
    def after_agent(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._check_plan(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._check_plan(state)

    def _check_plan(self, state: ArtifactWorkflowState) -> dict[str, Any] | None:
        if self.workflow != "planning":
            return None
        if state.get("agent_phase", "planning") != "planning":
            return None
        if self.artifacts.read_text(PLAN_BACKEND_KEY).strip():
            return None

        reminders = int(state.get("plan_reminders", 0) or 0)
        if reminders >= self.max_reminders:
            # Returning None falls through `after_agent` directly to LangGraph END
            # (`jump_to="end"` inside `after_agent` loops back to `exit_node`).
            return None
        prompt = (
            "You stopped before writing `/artifacts/implementation_plan.md`. "
            "Write `/artifacts/implementation_plan.md` using `write_file` before proceeding."
        )
        return {
            "plan_reminders": reminders + 1,
            "messages": [HumanMessage(content=prompt, name="artifact_workflow")],
            "jump_to": "model",
        }


class ArtifactWorkflowMiddleware(AgentMiddleware[ArtifactWorkflowState, ContextT, ResponseT]):
    """
    Outer workflow middleware:
      - `wrap_model_call`: injects active phase (`planning` / `execution`) & artifact templates
      - `wrap_tool_call` : enforces `[PLAN-FIRST REFUSAL]`, triggers `interrupt()` on
                           `request_review`, mirrors `write_todos` -> `/artifacts/task.md`,
                           and runs pre-flight guardrail path checks
      - `after_model`    : mirrors `state["todos"]` -> `/artifacts/task.md`
      - `after_agent`    : validates `/artifacts/walkthrough.md` and stamps deterministic
                           verification output into `Validation Results`
    """

    state_schema = ArtifactWorkflowState

    def __init__(
        self,
        *,
        workflow: WorkflowKind = "planning",
        plan_review: PlanReviewPolicy = "request_review",
        artifacts: ArtifactWorkspace,
        workspace_backend: BackendProtocol,
        tracker: GateRuntimeTracker,
        max_walkthrough_attempts: int = 2,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__()
        self.workflow = workflow
        self.plan_review = plan_review
        self.artifacts = artifacts
        self.workspace_backend = workspace_backend
        self.tracker = tracker
        self.max_walkthrough_attempts = max_walkthrough_attempts
        self.event_callback = event_callback

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback({"event_type": event_type, **payload})

    # ------------------------------------------------------------------ lifecycle
    def before_agent(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._init_state(state)

    async def abefore_agent(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._init_state(state)

    def _init_state(self, state: ArtifactWorkflowState) -> dict[str, Any] | None:
        if state.get("agent_phase") is not None:
            return None
        if self.workflow == "planning":
            return {"agent_phase": "planning", "plan_approved": False, "walkthrough_attempts": 0}
        return {"agent_phase": "execution", "plan_approved": True, "walkthrough_attempts": 0}

    # ------------------------------------------------------------------ model prompt
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        return handler(self._augment_model_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        return await handler(self._augment_model_request(request))

    def _augment_model_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        if self.workflow != "planning":
            return request
        state = request.state or {}
        phase = state.get("agent_phase") or "planning"
        extra = _PLANNING_PHASE_PROMPT if phase == "planning" else _EXECUTION_PHASE_PROMPT
        comments = state.get("plan_review_comments") or []
        if comments:
            joined = "\n".join(f"- {c}" for c in comments)
            extra = f"{extra}\n\n### User Plan Review Comments (Must Address)\n{joined}"
        return request.override(system_message=_append_system_text(request.system_message, extra))

    # ------------------------------------------------------------------ todo sync
    def after_model(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        self._sync_todos_from_state(state)
        return None

    async def aafter_model(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        self._sync_todos_from_state(state)
        return None

    def _sync_todos_from_state(self, state: Mapping[str, Any]) -> None:
        todos = state.get("todos")
        if isinstance(todos, Sequence) and todos:
            self.artifacts.write_text(TASK_BACKEND_KEY, render_task_markdown(todos))

    # ------------------------------------------------------------------ tool guard
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        pre_refusal = self._pre_tool_guard(request)
        if pre_refusal is not None:
            return pre_refusal
        result = handler(request)
        return self._post_tool_hook(request, result)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        pre_refusal = self._pre_tool_guard(request)
        if pre_refusal is not None:
            return pre_refusal
        result = await handler(request)
        return self._post_tool_hook(request, result)

    def _extract_tool_path(self, tool_name: str, args: Mapping[str, Any]) -> str | None:
        if tool_name in ("ls", "glob", "grep"):
            raw = args.get("path")
            return str(raw) if raw is not None else "/"
        for key in ("file_path", "path"):
            if args.get(key) is not None:
                return str(args[key])
        return None

    def _pre_tool_guard(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call
        tool_name = str(call.get("name", ""))
        tool_call_id = call.get("id")
        args = call.get("args") or {}
        state = request.state if isinstance(request.state, Mapping) else {}
        phase = state.get("agent_phase") or ("planning" if self.workflow == "planning" else "execution")

        # 1) Pre-flight worktree guardrail check for non-`/artifacts/` filesystem calls so
        #    `GuardedWorktreeBackend.blocked_attempts` & `[WORKTREE ISOLATION REFUSAL]` fire
        #    even before `FilesystemMiddleware`'s own path/permission checks.
        if tool_name in _FS_TOOL_OP_MAP:
            raw_path = self._extract_tool_path(tool_name, args)
            if not is_artifacts_virtual_path(raw_path):
                guard_fn = getattr(self.workspace_backend, "guard_tool_path", None)
                if callable(guard_fn):
                    refusal = guard_fn(_FS_TOOL_OP_MAP[tool_name], raw_path)
                    if refusal:
                        return ToolMessage(
                            content=str(refusal),
                            name=tool_name,
                            tool_call_id=tool_call_id,
                            status="error",
                        )

        # 2) PLANNING phase enforcement (`[PLAN-FIRST REFUSAL]`)
        if self.workflow == "planning" and phase == "planning":
            if tool_name in _EXEC_TOOLS:
                msg = (
                    f"{PLAN_FIRST_REFUSAL_PREFIX} Tool '{tool_name}' is disabled during PLANNING phase. "
                    f"Explore in read-only mode and write `{PLAN_VIRTUAL_PATH}` first."
                )
                self.tracker.plan_refusals.append({"tool": tool_name, "target": str(args)})
                self._emit("plan_first_refused", {"tool_name": tool_name, "target": str(args)})
                return ToolMessage(content=msg, name=tool_name, tool_call_id=tool_call_id, status="error")

            if tool_name in _FS_MUTATION_TOOLS:
                raw_path = self._extract_tool_path(tool_name, args) or ""
                if not is_artifacts_virtual_path(raw_path):
                    msg = (
                        f"{PLAN_FIRST_REFUSAL_PREFIX} Cannot modify workspace file '{raw_path}' "
                        f"during PLANNING phase. Write `{PLAN_VIRTUAL_PATH}` first."
                    )
                    self.tracker.plan_refusals.append({"tool": tool_name, "target": raw_path})
                    self._emit("plan_first_refused", {"tool_name": tool_name, "target": raw_path})
                    return ToolMessage(
                        content=msg, name=tool_name, tool_call_id=tool_call_id, status="error"
                    )
        return None

    def _post_tool_hook(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        tool_name = str(call.get("name", ""))
        args = call.get("args") or {}

        # Immediate deterministic sync for `write_todos` -> `/artifacts/task.md`
        if tool_name == "write_todos":
            todos = args.get("todos")
            if isinstance(todos, Sequence):
                task_md = render_task_markdown(todos)
                self.artifacts.write_text(TASK_BACKEND_KEY, task_md)
                self._emit("artifact_updated", {"artifact": "task", "path": TASK_VIRTUAL_PATH})
            return result

        # Track workspace mutations so verification gates know whether code changed
        if tool_name in _EXEC_TOOLS:
            self.tracker.workspace_mutations += 1
        elif tool_name in _FS_MUTATION_TOOLS:
            raw_path = self._extract_tool_path(tool_name, args) or ""
            if not is_artifacts_virtual_path(raw_path):
                if not (isinstance(result, ToolMessage) and result.status == "error"):
                    self.tracker.workspace_mutations += 1

        # Plan submission & review transition when `/artifacts/implementation_plan.md` is written
        if self.workflow == "planning" and tool_name in ("write_file", "edit_file"):
            raw_path = self._extract_tool_path(tool_name, args) or ""
            if normalize_virtual_path(raw_path) == PLAN_VIRTUAL_PATH:
                if isinstance(result, ToolMessage) and result.status == "error":
                    return result
                return self._handle_plan_written(request, result)

        if self.workflow == "planning" and tool_name in ("write_file", "edit_file"):
            raw_path = self._extract_tool_path(tool_name, args) or ""
            if normalize_virtual_path(raw_path) == WALKTHROUGH_VIRTUAL_PATH:
                self._emit("artifact_updated", {"artifact": "walkthrough", "path": WALKTHROUGH_VIRTUAL_PATH})

        return result

    def _handle_plan_written(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage | Command[Any]:
        state = request.state if isinstance(request.state, Mapping) else {}
        current_phase = state.get("agent_phase") or "planning"
        plan_md = self.artifacts.read_text(PLAN_BACKEND_KEY)
        self._emit("artifact_updated", {"artifact": "implementation_plan", "path": PLAN_VIRTUAL_PATH})

        if current_phase != "planning":
            return result

        proceed = True
        comments: list[str] = list(state.get("plan_review_comments") or [])

        if self.plan_review == "request_review":
            self._emit("awaiting_plan_review", {"path": PLAN_VIRTUAL_PATH})
            resume_payload = interrupt(
                {
                    "type": "plan_review",
                    "artifact_path": PLAN_VIRTUAL_PATH,
                    "plan_markdown": plan_md,
                }
            )
            proceed, new_comments = _parse_plan_review_resume(resume_payload)
            if new_comments:
                comments = [*comments, *new_comments]

        base_msg = (
            result
            if isinstance(result, ToolMessage)
            else ToolMessage(
                content=f"Updated file {PLAN_VIRTUAL_PATH}",
                name=str(request.tool_call.get("name", "write_file")),
                tool_call_id=request.tool_call.get("id"),
            )
        )

        if not proceed:
            feedback_lines = "\n".join(f"- {c}" for c in comments) or "- Revise the plan."
            revised_msg = ToolMessage(
                content=(
                    f"{base_msg.content}\n"
                    f"[USER PLAN REVIEW FEEDBACK — REVISE PLAN]\n{feedback_lines}\n"
                    f"Update `{PLAN_VIRTUAL_PATH}` to address the feedback before execution."
                ),
                name=base_msg.name,
                tool_call_id=base_msg.tool_call_id,
            )
            return Command(
                update={
                    "agent_phase": "planning",
                    "plan_approved": False,
                    "plan_review_comments": comments,
                    "messages": [revised_msg],
                }
            )

        note = "[PLAN APPROVED — PROCEED TO EXECUTION]"
        if comments:
            joined = "\n".join(f"- {c}" for c in comments)
            note += f"\n[USER PLAN REVIEW COMMENTS]\n{joined}"
        approved_msg = ToolMessage(
            content=f"{base_msg.content}\n{note}",
            name=base_msg.name,
            tool_call_id=base_msg.tool_call_id,
        )
        return Command(
            update={
                "agent_phase": "execution",
                "plan_approved": True,
                "plan_review_comments": comments,
                "messages": [approved_msg],
            }
        )

    # ------------------------------------------------------------------ walkthrough gate
    @hook_config(can_jump_to=["model"])
    def after_agent(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._finalize_walkthrough(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(
        self,
        state: ArtifactWorkflowState,
        runtime: Runtime[ContextT],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._finalize_walkthrough(state)

    def _finalize_walkthrough(self, state: ArtifactWorkflowState) -> dict[str, Any] | None:
        if self.workflow != "planning":
            return None
        if self.tracker.pytest_gate_failed_terminally:
            return None
        if state.get("agent_phase") == "planning":
            return None

        # Ensure final `todos` snapshot is persisted in `/artifacts/task.md`
        self._sync_todos_from_state(state)

        walkthrough_md = self.artifacts.read_text(WALKTHROUGH_BACKEND_KEY)
        attempts = int(state.get("walkthrough_attempts", 0) or 0)
        if not has_required_walkthrough_sections(walkthrough_md):
            if attempts < self.max_walkthrough_attempts:
                prompt = (
                    f"Verification passed, but `{WALKTHROUGH_VIRTUAL_PATH}` is missing or incomplete. "
                    f"Use `write_file` to create `{WALKTHROUGH_VIRTUAL_PATH}` with `# Walkthrough`, "
                    "`## Changes Made`, and `## Validation Results`, then finish."
                )
                return {
                    "walkthrough_attempts": attempts + 1,
                    "messages": [HumanMessage(content=prompt, name="artifact_workflow")],
                    "jump_to": "model",
                }

        last_gate = self.tracker.last_pytest_run
        if last_gate is not None:
            stamped = stamp_validation_results(
                walkthrough_md,
                command=str(last_gate["command"]),
                exit_code=int(last_gate["exit_code"]),
                output=str(last_gate["output"]),
            )
            self.artifacts.write_text(WALKTHROUGH_BACKEND_KEY, stamped)
            self._emit("artifact_updated", {"artifact": "walkthrough", "path": WALKTHROUGH_VIRTUAL_PATH})

        return {"agent_phase": "completed"}
