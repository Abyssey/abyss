"""
================================================================================
[MY LEARNING CORE #1] abyss/core/agent_runtime.py
Claude Code Tool-Use Execution Loop & Context Reset Handoff Protocol
================================================================================

[구현 메커니즘 포인트]
1. Direct Mode & Code Mode (`REPL`) 듀얼 디스패치:
   일반 Tool-Use 루프(매 턴 tool_calls 실행)뿐 아니라 `CodeModeExecutor` 주입 시
   도구 스키마를 단일 `REPL` 도구로 스왑하여, 코드 한 덩어리로 여러 도구를 실행하고
   마지막 표현식만 컨텍스트에 적재합니다.
   또한 Qwen 3.8 Flash 등 추론 모델의 `<think>...</think>` 블록을 본문과 분리해
   컨텍스트 오염 없이 추론 트레이스만 별도 스트림으로 추출합니다.

2. Context Compaction 대신 Context Reset + Structured Handoff Artifact:
   Anthropic의 "Effective Harnesses for Long-Running Agents" 및 "Three-Agent Harness"가
   지적하듯, 장기 실행 시 기존 대화를 요약 압축(Compaction)하면 모델이 컨텍스트 한계를
   의식해 작업을 조기 종료(premature termination)하거나 초기 스키마 제약을 유실합니다.
   따라서 턴 종료 시 전체 메시지 히스토리를 넘기는 대신 구조화된 `HandoffArtifact`
   (files_touched, blocked_attempts, tool_history)만 추출하여 다음 에이전트(Reviewer/Lead)가
   깨끗한 컨텍스트 창에서 시작하도록 설계했습니다.
================================================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from abyss.core.code_mode import REPL_TOOL_NAME, CodeModeExecutor
from abyss.core.isolation_policy import PolicyVerdict, WorktreeIsolationPolicy


@dataclass
class ToolCallRecord:
    """단일 도구 호출 및 Worktree 격리 검증 기록."""

    step: int
    tool_name: str
    arguments: dict[str, Any]
    isolation_verdict: PolicyVerdict
    output: str
    blocked: bool = False


@dataclass
class HandoffArtifact:
    """
    [Anthropic Long-Running Harness Pattern]
    컨텍스트 리셋(Context Reset) 후 다음 에이전트(Reviewer / Lead)에게 전달되는
    구조화된 작업 인수인계 아티팩트.
    """

    agent_id: str
    role: str
    worktree_branch: str
    summary: str
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
    Claude Code 스타일의 코어 에이전트 실행 루프.

    - OpenAI 호환 엔드포인트(사내 Qwen 3.8 Flash, Gemini, OpenAI, Mock)와 통신
    - Qwen 3.8 / 추론 모델의 `<think>...</think>` 블록 자동 분리 추출
    - 매 도구 실행 전 `WorktreeIsolationPolicy` 4대 가드레일 강제 적용
    - 루프 종료 시 다음 에이전트를 위한 `HandoffArtifact` 생성
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
        self.tools_registry = tools_registry
        self.tools_schema = tools_schema
        self.max_steps = max_steps
        self.event_callback = event_callback

        # ---------------------------------------------------------------------
        # [Code Mode, experimental] 도구 목록을 REPL 하나로 교체합니다.
        # 모델은 개별 도구 대신 "도구를 호출하는 코드"를 보내고, 코드 안의 각 호출은
        # CodeModeExecutor가 평소 이름(Bash/Edit/...)으로 권한·격리·훅 검사를 합니다.
        # ---------------------------------------------------------------------
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
        """
        Qwen 3.8 Flash 등 추론 모델이 반환하는 `<think>...</think>` 태그를
        본문 응답에서 분리하여 UI에서 사고 과정(Thinking)을 별도 표시할 수 있게 합니다.
        """
        if not raw_text:
            return [], ""
        thoughts = [m.strip() for m in cls._THINK_REGEX.findall(raw_text) if m.strip()]
        cleaned = cls._THINK_REGEX.sub("", raw_text).strip()
        return thoughts, cleaned

    def _verify_tool_isolation(
        self, tool_name: str, args: dict[str, Any]
    ) -> PolicyVerdict:
        """
        [핵심 연결부] 도구 종류에 따라 WorktreeIsolationPolicy의 4대 검증을 호출합니다.
        """
        if tool_name == REPL_TOOL_NAME and self.code_mode is not None:
            # Code Mode: 코드 덩어리 자체에 대한 REPL PreToolUse 검토.
            # 코드 안의 개별 호출은 실행 중에 CodeModeExecutor가 하나씩 다시 검사합니다.
            review = self.code_mode.review_code(args.get("code", ""))
            return PolicyVerdict(
                allowed=review.allowed,
                check_name="repl_code_review",
                reason="REPL code passed static review." if review.allowed else "; ".join(review.findings),
                remediation_hint="import/dunder 없이 sh/read/write/edit/ls/gather 도구 API만 사용하세요.",
            )

        if tool_name in ("write_file", "edit_file", "delete_file"):
            target = args.get("path") or args.get("file_path") or ""
            return self.policy.validate_file_edit(target)

        if tool_name == "run_command":
            command = args.get("command", "")
            cwd = args.get("cwd")
            return self.policy.validate_shell_command(command=command, cwd=cwd)

        # 읽기 전용 도구 또는 메일박스/태스크 보드 도구는 통과
        return PolicyVerdict(
            allowed=True,
            check_name="non_destructive_tool",
            reason=f"Tool '{tool_name}' is safe inside harness.",
        )

    def run(
        self,
        system_prompt: str,
        task_prompt: str,
        llm_callable: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]],
    ) -> HandoffArtifact:
        """
        =======================================================================
        [HANDS-ON CORE LOOP] 에이전트 실행 루프 (STEP 1 ~ STEP 4)
        =======================================================================
        발표에서 직접 코드 흐름을 설명할 핵심 4단계 루프입니다:
          STEP 1: 대화 컨텍스트(messages)와 도구 스키마(tools_schema)로 LLM 호출
          STEP 2: `<think>` 추론 블록 분리 및 `tool_calls` 유무 확인 (없으면 완료)
          STEP 3: 각 `tool_call`에 대해 `WorktreeIsolationPolicy` 사전 검증 수행
                  (차단 시 실행하지 않고 Claude Code 방식의 교정 안내 에러를 반환)
          STEP 4: 도구 실행 결과를 `role: "tool"` 메시지로 히스토리에 추가 후 다음 턴 진행
        =======================================================================
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_prompt},
        ]

        reasoning_trace: list[str] = []
        files_touched: list[str] = []
        tool_history: list[ToolCallRecord] = []
        blocked_attempts = 0
        final_summary = ""
        provider_used = "unknown"

        for step in range(1, self.max_steps + 1):
            # -----------------------------------------------------------------
            # STEP 1: LLM 호출 (Provider Rotator를 통해 Qwen 3.8 / Gemini / OpenAI 호출)
            # -----------------------------------------------------------------
            response = llm_callable(messages, self.tools_schema)
            provider_used = response.get("provider", provider_used)
            raw_content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []

            # -----------------------------------------------------------------
            # STEP 2: <think> 추론 분리 및 종료 조건 검사
            # -----------------------------------------------------------------
            thoughts, clean_content = self.extract_thinking_and_content(raw_content)
            for thought in thoughts:
                reasoning_trace.append(thought)
                self._emit("thinking", {"step": step, "thought": thought, "provider": provider_used})

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": clean_content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # 더 이상 호출할 도구가 없으면 작업 완료로 판정하고 루프 종료
            if not tool_calls:
                final_summary = clean_content or "Task completed."
                self._emit(
                    "completed",
                    {"step": step, "summary": final_summary, "provider": provider_used},
                )
                break

            # -----------------------------------------------------------------
            # STEP 3 & 4: Worktree 격리 검증 후 도구 실행 및 결과 피드백
            # -----------------------------------------------------------------
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
            reasoning_trace=reasoning_trace,
            files_touched=files_touched,
            tool_history=tool_history,
            blocked_attempts=blocked_attempts,
            provider_used=provider_used,
        )
