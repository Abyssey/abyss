"""
================================================================================
Focus Domain #2 · app/domains/execution/runtime.py
Dual-Mode Agent Execution Runtime (Direct Tools & Code Mode REPL)
================================================================================

[설계 의도]
1. Direct Tool Calling & Code Mode (`REPL`) 듀얼 디스패치:
   일반 Tool-Use 루프(매 턴 tool_calls 실행)뿐 아니라 `CodeModeExecutor` 주입 시
   도구 스키마를 단일 `REPL` 도구로 전환하여, 코드 한 덩어리로 여러 도구를 실행하고
   마지막 표현식만 컨텍스트에 적재합니다.
   또한 Qwen 3.8 Flash 등 추론 모델의 `<think>...</think>` 블록을 본문과 분리해
   컨텍스트 오염 없이 추론 트레이스만 별도 스트림으로 추출합니다.

2. 선택적 컨텍스트 전략 (`context_strategy: "compaction" | "reset_handoff"`):
   - Claude Code CLI의 기본 컨텍스트 관리는 **Auto-Compaction** (`/compact`, `CLAUDE.md`)입니다.
   - 장기 실행이나 에이전트 간 인계(Worker → Independent Reviewer)에서는 대안 전략으로
     **Context Reset (`/clear`) + Structured `HandoffArtifact`** 를 선택해
     수정 파일·가드레일 차단 횟수·도구 이력만 깔끔하게 인계할 수 있습니다.
   - `AgentRuntime`은 두 전략을 `context_strategy` 인자로 명시적으로 선택할 수 있게 설계했습니다.
================================================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from app.domains.execution.code_mode import REPL_TOOL_NAME, CodeModeExecutor
from app.domains.guardrails.isolation_policy import PolicyVerdict, WorktreeIsolationPolicy
from app.domains.guardrails.permissions import PermissionRules

ContextStrategy = Literal["compaction", "reset_handoff"]


@dataclass
class ToolCallRecord:
    """단일 도구 호출 및 가드레일 검증 기록."""

    step: int
    tool_name: str
    arguments: dict[str, Any]
    isolation_verdict: PolicyVerdict
    output: str
    blocked: bool = False


@dataclass
class HandoffArtifact:
    """
    작업 완료 후 다음 에이전트(Reviewer / Lead)에게 전달되는 구조화된 인수인계 아티팩트.
    """

    agent_id: str
    role: str
    worktree_branch: str
    summary: str
    context_strategy: ContextStrategy = "compaction"
    compaction_events: int = 0
    reasoning_trace: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    tool_history: list[ToolCallRecord] = field(default_factory=list)
    blocked_attempts: int = 0
    provider_used: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "worktree_branch": self.worktree_branch,
            "summary": self.summary,
            "context_strategy": self.context_strategy,
            "compaction_events": self.compaction_events,
            "reasoning_trace": self.reasoning_trace,
            "files_touched": self.files_touched,
            "blocked_attempts": self.blocked_attempts,
            "provider_used": self.provider_used,
            "tool_calls_count": len(self.tool_history),
            "tool_history": [
                {
                    "step": t.step,
                    "tool_name": t.tool_name,
                    "arguments": t.arguments,
                    "allowed": t.isolation_verdict.allowed,
                    "check_name": t.isolation_verdict.check_name,
                    "reason": t.isolation_verdict.reason,
                    "output_preview": t.output[:400],
                    "blocked": t.blocked,
                }
                for t in self.tool_history
            ],
        }


class AgentRuntime:
    """
    코어 코딩 에이전트 실행 루프 (Pure Python Stdlib + `app.domains.guardrails`).

    - OpenAI 호환 엔드포인트(사내 Qwen 3.8 Flash, Gemini, OpenAI, Mock)와 통신
    - 추론 모델의 `<think>...</think>` 블록 자동 분리 추출
    - 매 도구 실행 전 `WorktreeIsolationPolicy` 4대 가드레일 (+ 선택적 `PermissionRules`) 강제 적용
    - `context_strategy` ("compaction" 기본값 vs "reset_handoff" 선택 인계 모드) 지원
    """

    _THINK_REGEX = re.compile(r"<think>(.*?)</think>", re.DOTALL)

    def __init__(
        self,
        agent_id: str,
        role: str,
        worktree_root: str | Path,
        main_checkout_root: str | Path,
        worktree_branch: str,
        tools_registry: dict[str, Callable[..., str]],
        tools_schema: list[dict[str, Any]],
        max_steps: int = 12,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        code_mode: CodeModeExecutor | None = None,
        context_strategy: ContextStrategy = "compaction",
        permissions: PermissionRules | None = None,
        compaction_message_limit: int = 10,
        ask_handler: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.worktree_root = Path(worktree_root).resolve()
        self.main_checkout_root = Path(main_checkout_root).resolve()
        self.worktree_branch = worktree_branch
        self.policy = WorktreeIsolationPolicy(
            worktree_root=self.worktree_root,
            main_checkout_root=self.main_checkout_root,
        )
        self.permissions = permissions
        self.ask_handler = ask_handler
        self.context_strategy: ContextStrategy = context_strategy
        self.compaction_message_limit = max(6, compaction_message_limit)
        self.tools_registry = tools_registry
        self.tools_schema = tools_schema
        self.max_steps = max_steps
        self.event_callback = event_callback

        self.code_mode = code_mode
        if code_mode is not None:
            if code_mode.event_callback is None:
                code_mode.agent_id = agent_id
                code_mode.event_callback = lambda evt: self._emit(evt.pop("event_type"), evt)
            self.tools_schema = [code_mode.tool_schema()]
            self.tools_registry = {REPL_TOOL_NAME: lambda code: code_mode.run(code).model_view()}

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(
                {
                    "agent_id": self.agent_id,
                    "role": self.role,
                    "branch": self.worktree_branch,
                    "event_type": event_type,
                    **payload,
                }
            )

    @classmethod
    def extract_thinking_and_content(cls, raw_text: str | None) -> tuple[list[str], str]:
        """추론 모델이 반환하는 `<think>...</think>` 태그를 본문 응답에서 분리합니다."""
        if not raw_text:
            return [], ""
        thoughts = [m.strip() for m in cls._THINK_REGEX.findall(raw_text) if m.strip()]
        cleaned = cls._THINK_REGEX.sub("", raw_text).strip()
        return thoughts, cleaned

    def _normalize_target_for_permissions(self, raw_target: str) -> str:
        if not raw_target or "\x00" in raw_target:
            return raw_target
        p = Path(raw_target)
        resolved = (self.worktree_root / p).resolve() if not p.is_absolute() else p.resolve()
        if resolved.is_relative_to(self.worktree_root):
            rel = resolved.relative_to(self.worktree_root).as_posix()
            return rel or "."
        return resolved.as_posix()

    def _check_permission(
        self, perm_tool: str, subject: str, args: dict[str, Any]
    ) -> PolicyVerdict | None:
        if self.permissions is None:
            return None
        perm = self.permissions.evaluate(perm_tool, subject)
        if perm.decision == "deny":
            return PolicyVerdict(
                allowed=False,
                check_name="permission_deny",
                reason=perm.reason,
                remediation_hint=f"{perm_tool} 호출이 권한 규칙({perm.rule})에 의해 차단되었습니다.",
            )
        if perm.decision == "ask":
            payload = {"tool_name": perm_tool, "tool_input": args, "rule": perm.rule}
            approved = bool(self.ask_handler(payload)) if self.ask_handler else False
            if not approved:
                return PolicyVerdict(
                    allowed=False,
                    check_name="permission_ask",
                    reason=(
                        f"{perm.reason} — no approver in this unattended session, so the call is refused."
                    ),
                    remediation_hint="사전 승인(ask_handler)이 없는 비대화형 세션에서는 ask 규칙 호출이 차단됩니다.",
                )
        return None

    def _verify_tool_isolation(
        self, tool_name: str, args: dict[str, Any]
    ) -> PolicyVerdict:
        """도구 종류에 따라 `PermissionRules` 및 `WorktreeIsolationPolicy` 4대 검증을 호출합니다."""
        if tool_name == REPL_TOOL_NAME and self.code_mode is not None:
            review = self.code_mode.review_code(args.get("code", ""))
            return PolicyVerdict(
                allowed=review.allowed,
                check_name="repl_code_review",
                reason="REPL code passed static review." if review.allowed else "; ".join(review.findings),
                remediation_hint="import/dunder 없이 sh/read/write/edit/ls/gather 도구 API만 사용하세요.",
            )

        if tool_name in ("write_file", "edit_file", "delete_file"):
            targets = [
                str(args[k]) for k in ("path", "file_path") if args.get(k) is not None
            ] or [""]
            perm_tool = "Write" if tool_name == "write_file" else "Edit"
            last_verdict: PolicyVerdict | None = None
            for target in targets:
                norm_target = self._normalize_target_for_permissions(target)
                perm_verdict = self._check_permission(perm_tool, norm_target, args)
                if perm_verdict is not None:
                    return perm_verdict
                verdict = self.policy.validate_file_edit(target)
                if not verdict.allowed:
                    return verdict
                last_verdict = verdict
            return last_verdict or self.policy.validate_file_edit("")

        if tool_name == "read_file":
            targets = [
                str(args[k]) for k in ("path", "file_path") if args.get(k) is not None
            ] or [""]
            last_verdict = None
            for target in targets:
                norm_target = self._normalize_target_for_permissions(target)
                perm_verdict = self._check_permission("Read", norm_target, args)
                if perm_verdict is not None:
                    return perm_verdict
                verdict = self.policy.validate_file_read(target)
                if not verdict.allowed:
                    return verdict
                last_verdict = verdict
            return last_verdict or self.policy.validate_file_read("")

        if tool_name == "list_files":
            targets = [
                str(args[k])
                for k in ("subdir", "directory", "path")
                if args.get(k) is not None
            ] or ["."]
            last_verdict = None
            for target in targets:
                norm_target = self._normalize_target_for_permissions(target)
                perm_verdict = self._check_permission("Glob", norm_target, args)
                if perm_verdict is not None:
                    return perm_verdict
                verdict = self.policy.validate_file_read(target)
                if not verdict.allowed:
                    return verdict
                last_verdict = verdict
            return last_verdict or self.policy.validate_file_read(".")

        if tool_name == "run_command":
            command = str(args.get("command", ""))
            cwd = args.get("cwd")
            perm_verdict = self._check_permission("Bash", command, args)
            if perm_verdict is not None:
                return perm_verdict
            return self.policy.validate_shell_command(command=command, cwd=cwd)

        return PolicyVerdict(
            allowed=True,
            check_name="non_destructive_tool",
            reason=f"Tool '{tool_name}' is safe inside harness.",
        )

    def _compact_messages(
        self, messages: list[dict[str, Any]], files_touched: list[str]
    ) -> list[dict[str, Any]]:
        """Auto-Compaction 전략: 오래된 도구 출력을 요약 노트로 압축해 컨텍스트 창을 유지합니다."""
        if len(messages) <= self.compaction_message_limit:
            return messages
        head = messages[:2]
        tail = messages[-4:]
        dropped_count = len(messages) - len(head) - len(tail)
        summary_msg = {
            "role": "system",
            "content": (
                f"[AUTO-COMPACT SUMMARY] Compacted {dropped_count} earlier turns. "
                f"Files touched so far: {', '.join(files_touched) or 'none'}."
            ),
        }
        return [*head, summary_msg, *tail]

    def run(
        self,
        system_prompt: str,
        task_prompt: str,
        llm_callable: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]],
    ) -> HandoffArtifact:
        """
        핵심 에이전트 실행 루프 (STEP 1 ~ STEP 4):
          STEP 1: 대화 컨텍스트(messages)와 도구 스키마(tools_schema)로 LLM 호출
          STEP 2: `<think>` 추론 블록 분리 및 `tool_calls` 유무 확인 (없으면 완료)
          STEP 3: 각 `tool_call`에 대해 `WorktreeIsolationPolicy` (+ `PermissionRules`) 사전 검증
          STEP 4: 도구 실행 결과를 `role: "tool"` 메시지로 피드백 후 다음 턴 진행
                  (`context_strategy="compaction"` 시 일정 턴 이상 자동 요약 압축 적용)
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_prompt},
        ]

        reasoning_trace: list[str] = []
        files_touched: list[str] = []
        tool_history: list[ToolCallRecord] = []
        blocked_attempts = 0
        compaction_events = 0
        final_summary = ""
        provider_used = "unknown"

        for step in range(1, self.max_steps + 1):
            if self.context_strategy == "compaction" and len(messages) > self.compaction_message_limit:
                messages = self._compact_messages(messages, files_touched)
                compaction_events += 1
                self._emit("context_compacted", {"step": step, "compaction_events": compaction_events})

            response = llm_callable(messages, self.tools_schema)
            provider_used = response.get("provider", provider_used)
            raw_content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []

            thoughts, clean_content = self.extract_thinking_and_content(raw_content)
            for thought in thoughts:
                reasoning_trace.append(thought)
                self._emit("thinking", {"step": step, "thought": thought, "provider": provider_used})

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": clean_content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                final_summary = clean_content or "Task completed."
                self._emit(
                    "completed",
                    {"step": step, "summary": final_summary, "provider": provider_used},
                )
                break

            for tc in tool_calls:
                tc_id = tc.get("id", f"call_{step}")
                func_info = tc.get("function", {})
                tool_name = func_info.get("name", "")
                raw_args = func_info.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}

                verdict = self._verify_tool_isolation(tool_name, args)

                if not verdict.allowed:
                    blocked_attempts += 1
                    tool_output = (
                        f"[WORKTREE ISOLATION REFUSAL - {verdict.check_name}] "
                        f"{verdict.reason}\nHow to proceed: {verdict.remediation_hint}"
                    )
                    record = ToolCallRecord(
                        step=step,
                        tool_name=tool_name,
                        arguments=args,
                        isolation_verdict=verdict,
                        output=tool_output,
                        blocked=True,
                    )
                    self._emit(
                        "isolation_blocked",
                        {
                            "step": step,
                            "tool_name": tool_name,
                            "check_name": verdict.check_name,
                            "reason": verdict.reason,
                            "remediation": verdict.remediation_hint,
                        },
                    )
                else:
                    tool_fn = self.tools_registry.get(tool_name)
                    if tool_fn is None:
                        tool_output = f"Error: Unknown tool '{tool_name}'."
                    else:
                        try:
                            tool_output = str(tool_fn(**args))
                            if tool_name in ("write_file", "edit_file"):
                                target_file = args.get("path") or args.get("file_path")
                                if target_file and target_file not in files_touched:
                                    files_touched.append(str(target_file))
                            if tool_name == REPL_TOOL_NAME and self.code_mode and self.code_mode.last_result:
                                for f in self.code_mode.last_result.files_touched:
                                    if f not in files_touched:
                                        files_touched.append(f)
                        except Exception as exc:  # noqa: BLE001
                            tool_output = f"Tool execution error in '{tool_name}': {exc}"

                    record = ToolCallRecord(
                        step=step,
                        tool_name=tool_name,
                        arguments=args,
                        isolation_verdict=verdict,
                        output=tool_output,
                        blocked=False,
                    )
                    self._emit(
                        "tool_executed",
                        {
                            "step": step,
                            "tool_name": tool_name,
                            "arguments": args,
                            "check_name": verdict.check_name,
                            "output_preview": tool_output[:300],
                        },
                    )

                tool_history.append(record)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": tool_name,
                        "content": tool_output,
                    }
                )
        else:
            final_summary = "Reached maximum step limit; handing off current state."

        return HandoffArtifact(
            agent_id=self.agent_id,
            role=self.role,
            worktree_branch=self.worktree_branch,
            summary=final_summary,
            context_strategy=self.context_strategy,
            compaction_events=compaction_events,
            reasoning_trace=reasoning_trace,
            files_touched=files_touched,
            tool_history=tool_history,
            blocked_attempts=blocked_attempts,
            provider_used=provider_used,
        )
