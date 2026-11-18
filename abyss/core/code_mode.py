r"""
================================================================================
[MY LEARNING CORE #4] abyss/core/code_mode.py
Claude Code "Code Mode" (v2.1.323+, experimental) — 모델이 코드를 써서 한 스텝에 여러 tool call 실행
================================================================================

[무엇이 바뀌었나? — Direct Tool Calling vs Code Mode]
  * Direct (기존 방식, agent_runtime.py의 기본 루프):
      LLM → tool_call 1개 → 결과를 읽고 → LLM → tool_call 1개 → ...
      tool call N개 = LLM 왕복 N번. 매 왕복마다 지금까지의 전체 대화(도구 출력 원문 포함)를 다시 보냄.
  * Code Mode (Claude Code v2.1.323+, `CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1`):
      LLM → `REPL` 도구에 "코드 한 덩어리" → 런타임이 실행하며 그 안에서 여러 tool call 수행
        - 독립 명령은 병렬 실행 (JS의 Promise.all ↔ 여기서는 `await gather(...)`)
        - 앞 결과를 다음 호출 인자로 바로 사용 (grep 결과 → edit 대상)
        - 마지막 표현식만 모델에게 반환 → 모델은 "필요한 부분"만 읽음 (컨텍스트 절약)

  Claude Code는 JavaScript(`$\`rg ...\``, `edit(...)`)를 쓰지만, Abyss는 사내 Qwen 3.8 Flash가
  가장 잘 쓰는 언어이자 발표자가 익숙한 Python으로 동일한 구조를 구현합니다.

      Claude Code (JS)                          Abyss (Python)
      ----------------------------------------  ------------------------------------------
      const f = await $`rg -l "x" src`.text();  f = (await sh('grep -rl "x" src')).text
      await edit(t, "a", "b");                  await edit(t, "a", "b")
      $`npm test`.nothrow()                     await sh("pytest -q", nothrow=True)
      ({ edited: t, tests: ... });  (마지막 식)  {"edited": t, "tests": ...}   (마지막 식)

[안전 설계 — 문서의 "Permissions and long-running code" 절을 그대로 코드로 옮김]
  STEP 0  REPL PreToolUse  : 코드 자체를 실행 전에 정적 검토 (문서: hook matching `REPL`,
                             `tool_input.code`를 읽음). ⚠ 런타임에 계산되는 값(검색 결과 경로 등)은
                             볼 수 없으므로 이것만으로는 부족 → STEP 2가 필수.
  STEP 1  Compile          : 마지막 식 → return 변환 + 모든 루프/함수에 예산 tick 주입
  STEP 2  Per-call Gate    : 코드 안의 tool call 하나하나가 평소 이름(Bash/Read/Edit/Write)으로
                             ① deny/ask 규칙(복합 명령은 세그먼트별) ② WorktreeIsolationPolicy
                             ③ PreToolUse 훅(timeout · onFailure)을 통과해야 실행
  STEP 3  Foreground run   : 기본 10분 제한 (문서: "it stops after ten minutes")
  STEP 4  Background run   : 최대 2시간, 별도 스레드 + poll (문서: "up to two hours")

[한계 — 정직하게]
  AST 정적 검토는 보안 경계가 아닙니다. 실제 경계는 STEP 2의 경로 기반 격리와 OS 샌드박스입니다.
================================================================================
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from abyss.core.isolation_policy import WorktreeIsolationPolicy
from abyss.core.permissions import PermissionRules, split_compound_command

REPL_TOOL_NAME = "REPL"
FOREGROUND_LIMIT_S = 600.0  # 10 minutes
BACKGROUND_LIMIT_S = 7200.0  # 2 hours
MAX_LOOP_TICKS = 200_000
MAX_RANGE = 100_000


# =============================================================================
# 코드 안에서 잡을 수 있는 예외들 (모델이 try/except로 처리 가능)
# =============================================================================
class ToolRefused(Exception):
    """deny 규칙 / 격리 정책 / 훅에 의해 tool call이 거부됨."""


class BudgetExceeded(Exception):
    """시간·루프 예산 초과 (foreground 10분 / background 2시간)."""


@dataclass
class ShellResult:
    r"""`await sh(...)`의 반환값. zx(`$\`...\``)의 ProcessOutput과 같은 역할."""

    command: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def text(self) -> str:
        return self.stdout

    def lines(self) -> list[str]:
        return [ln for ln in self.stdout.splitlines() if ln.strip()]

    def tail(self, n: int = 20) -> str:
        return "\n".join((self.stdout + self.stderr).strip().splitlines()[-n:])

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "exit_code": self.exit_code, "output_tail": self.tail(12)}


class CommandFailed(Exception):
    """`nothrow=False`(기본)인 sh()가 0이 아닌 exit code를 받으면 발생 (zx와 동일한 의미)."""

    def __init__(self, result: ShellResult) -> None:
        self.result = result
        super().__init__(
            f"Command failed (exit {result.exit_code}): {result.command}\n{result.tail(12)}"
        )


# =============================================================================
# 기록용 데이터 구조
# =============================================================================
@dataclass
class HookSpec:
    """
    PreToolUse 훅 정의. matcher는 도구 이름 정규식("Bash", "Edit|Write", "REPL").
    fn(payload) → None(의견 없음) | {"decision": "deny"|"allow", "reason": str}
    on_failure="block"(기본): 훅이 예외/타임아웃이면 호출 거부 — 문서의
      "the hook failed" / "the hook did not finish" 동작.
    """

    name: str
    matcher: str
    fn: Callable[[dict[str, Any]], dict[str, Any] | None]
    timeout_s: float = 5.0
    on_failure: Literal["block", "continue"] = "block"


@dataclass
class CodeReview:
    allowed: bool
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodeToolCall:
    """코드 안에서 일어난 tool call 1건. UI에는 코드 대신 이 기록(=diff 등)이 보입니다."""

    seq: int
    tool: str
    arguments: dict[str, Any]
    allowed: bool
    decided_by: str
    reason: str
    output_preview: str = ""
    diff: str = ""
    started_ms: int = 0
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodeRunResult:
    run_id: str
    code: str
    status: str = "running"  # running | ok | error | timeout | refused_by_review
    background: bool = False
    review: CodeReview | None = None
    tool_calls: list[CodeToolCall] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    return_value: Any = None
    return_text: str = ""
    error: str = ""
    duration_ms: int = 0

    @property
    def files_touched(self) -> list[str]:
        out: list[str] = []
        for c in self.tool_calls:
            if c.allowed and c.tool in ("Edit", "Write"):
                p = str(c.arguments.get("path", ""))
                if p and p not in out:
                    out.append(p)
        return out

    def model_view(self) -> str:
        """REPL tool 결과로 모델에게 돌려줄 텍스트 — 마지막 식의 값(또는 에러)만."""
        head = f"[code-mode {self.status} · {len(self.tool_calls)} tool calls · {self.duration_ms}ms]"
        if self.status == "ok":
            return f"{head}\n{self.return_text}"
        refused = [f"- {c.tool} refused by {c.decided_by}: {c.reason}" for c in self.tool_calls if not c.allowed]
        findings = [f"- review: {f}" for f in (self.review.findings if self.review else [])]
        return "\n".join([head, self.error, *refused, *findings]).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "code": self.code,
            "status": self.status,
            "background": self.background,
            "review": self.review.to_dict() if self.review else None,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "logs": self.logs[-50:],
            "return_value": self.return_value,
            "return_text": self.return_text,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "files_touched": self.files_touched,
            "chars_returned_to_model": len(self.model_view()),
            "model_view": self.model_view(),
        }


# =============================================================================
# STEP 0 — REPL PreToolUse: 코드 정적 검토
# =============================================================================
_FORBIDDEN_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "breakpoint", "input", "exit", "quit", "help",
        "memoryview", "type", "object", "super", "classmethod", "staticmethod",
    }
)


class _CodeReviewer(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[str] = []

    def _flag(self, node: ast.AST, msg: str) -> None:
        self.findings.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    def visit_Import(self, node: ast.Import) -> None:
        self._flag(node, "import is not allowed — use the tool API (sh/read/write/edit/ls/gather).")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._flag(node, "from-import is not allowed — use the tool API.")

    def visit_Global(self, node: ast.Global) -> None:
        self._flag(node, "global statements are not allowed.")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._flag(node, "nonlocal statements are not allowed.")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._flag(node, "class definitions are not allowed in Code Mode snippets.")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            self._flag(node, f"private/dunder attribute access '.{node.attr}' is not allowed.")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _FORBIDDEN_NAMES or node.id.startswith("__"):
            self._flag(node, f"name '{node.id}' is not available in Code Mode.")


# =============================================================================
# STEP 1 — Compile: 마지막 식 반환 + 예산 tick 주입
# =============================================================================
class _BudgetInjector(ast.NodeTransformer):
    """모든 for/while/함수 본문 맨 앞에 `__abyss_tick__()`를 넣어 무한 루프를 끊습니다."""

    @staticmethod
    def _tick() -> ast.stmt:
        return ast.Expr(value=ast.Call(func=ast.Name(id="__abyss_tick__", ctx=ast.Load()), args=[], keywords=[]))

    def _inject(self, node: Any) -> Any:
        self.generic_visit(node)
        node.body.insert(0, self._tick())
        return node

    visit_For = visit_AsyncFor = visit_While = _inject
    visit_FunctionDef = visit_AsyncFunctionDef = _inject


def compile_program(code: str) -> Any:
    tree = ast.parse(code, mode="exec")
    body = list(tree.body)
    if body and isinstance(body[-1], ast.Expr):
        # JS 예제처럼 "마지막 줄의 값"이 모델에게 반환됩니다.
        body[-1] = ast.copy_location(ast.Return(value=body[-1].value), body[-1])
    wrapper = ast.parse("async def __abyss_main__():\n    pass\n")
    wrapper.body[0].body = body or [ast.Pass()]  # type: ignore[attr-defined]
    wrapper = _BudgetInjector().visit(wrapper)
    ast.fix_missing_locations(wrapper)
    return compile(wrapper, "<code-mode>", "exec")


# =============================================================================
# 실행 컨텍스트 (run 1회 = 코드 1덩어리)
# =============================================================================
class _RunContext:
    def __init__(self, executor: "CodeModeExecutor", result: CodeRunResult, limit_s: float) -> None:
        self.ex = executor
        self.result = result
        self.t0 = time.monotonic()
        self.deadline = self.t0 + limit_s
        self.ticks = 0
        self.seq = 0

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks > MAX_LOOP_TICKS:
            raise BudgetExceeded(f"loop budget exceeded ({MAX_LOOP_TICKS} iterations)")
        if time.monotonic() > self.deadline:
            raise BudgetExceeded("time budget exceeded")

    def remaining(self) -> float:
        return max(0.5, self.deadline - time.monotonic())

    # ---------------------------------------------------------------------
    # STEP 2 — Per-call Gate: deny/ask → isolation → PreToolUse hooks
    # ---------------------------------------------------------------------
    async def gate(self, tool: str, args: dict[str, Any]) -> tuple[bool, str, str]:
        ex = self.ex
        subject = args.get("command", "") if tool == "Bash" else ex.rel(args.get("path", "."))

        # ① deny / ask 규칙 (Bash는 복합 명령을 세그먼트별로)
        perm = ex.permissions.evaluate(tool, subject)
        if perm.decision == "deny":
            return False, "permission:deny", perm.reason
        if perm.decision == "ask":
            payload = {"tool_name": tool, "tool_input": args, "rule": perm.rule}
            approved = bool(ex.ask_handler(payload)) if ex.ask_handler else False
            if not approved:
                return False, "permission:ask", (
                    f"{perm.reason} — no approver in this unattended session, so the call is refused "
                    "(Claude Code blocks it when the approval countdown ends)."
                )

        # ② Worktree 격리 정책 (경로 기반 — 명령 텍스트 우회에 강함)
        if tool == "Bash":
            virtual_cwd = ex.worktree_root / args["cwd"] if args.get("cwd") else ex.worktree_root
            for seg in split_compound_command(args.get("command", "")):
                verdict = ex.policy.validate_shell_command(seg, cwd=virtual_cwd)
                if not verdict.allowed:
                    return False, f"isolation:{verdict.check_name}", verdict.reason
                m = re.match(r"^cd\s+(['\"]?)([^'\"]+)\1\s*$", seg)
                if m:
                    virtual_cwd = (virtual_cwd / m.group(2)).resolve()
        elif tool in ("Edit", "Write"):
            verdict = ex.policy.validate_file_edit(args.get("path", ""))
            if not verdict.allowed:
                return False, f"isolation:{verdict.check_name}", verdict.reason
        else:  # Read / Glob: 읽기도 이 랩에서는 worktree 내부로 한정
            if not ex.resolve(args.get("path", ".")).is_relative_to(ex.worktree_root):
                return False, "isolation:read_confinement", f"Read outside worktree: {args.get('path')}"

        # ③ PreToolUse 훅 (timeout · onFailure)
        ok, who, why = await ex.run_hooks(tool, args)
        if not ok:
            return False, who, why
        return True, "allowed", "passed permission rules, worktree isolation and PreToolUse hooks"

    async def call(self, tool: str, args: dict[str, Any], impl: Callable[[], Any]) -> Any:
        self.tick()
        self.seq += 1
        started = int((time.monotonic() - self.t0) * 1000)
        record = CodeToolCall(seq=self.seq, tool=tool, arguments=args, allowed=False, decided_by="", reason="", started_ms=started)
        self.result.tool_calls.append(record)

        allowed, who, why = await self.gate(tool, args)
        record.allowed, record.decided_by, record.reason = allowed, who, why
        if not allowed:
            record.duration_ms = int((time.monotonic() - self.t0) * 1000) - started
            self.ex.emit("code_mode_tool_refused", record.to_dict())
            raise ToolRefused(f"{tool} refused by {who}: {why}")

        try:
            value = await asyncio.to_thread(impl)  # 스레드로 → gather()가 진짜 병렬
        finally:
            record.duration_ms = int((time.monotonic() - self.t0) * 1000) - started
        if isinstance(value, tuple):  # (반환값, diff)
            value, record.diff = value
        record.output_preview = (value.tail(8) if isinstance(value, ShellResult) else str(value))[:600]
        self.ex.emit("code_mode_tool", record.to_dict())
        return value


# =============================================================================
# CodeModeExecutor — REPL 도구 구현체
# =============================================================================
class CodeModeExecutor:
    def __init__(
        self,
        worktree_root: str | Path,
        main_checkout_root: str | Path,
        permissions: PermissionRules | None = None,
        hooks: list[HookSpec] | None = None,
        ask_handler: Callable[[dict[str, Any]], bool] | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        foreground_timeout_s: float = FOREGROUND_LIMIT_S,
        background_timeout_s: float = BACKGROUND_LIMIT_S,
        max_return_chars: int = 4000,
        agent_id: str = "code-mode",
    ) -> None:
        self.worktree_root = Path(worktree_root).resolve()
        self.main_checkout_root = Path(main_checkout_root).resolve()
        self.policy = WorktreeIsolationPolicy(self.worktree_root, self.main_checkout_root)
        self.permissions = permissions or PermissionRules.default()
        self.hooks = hooks or []
        self.ask_handler = ask_handler
        self.event_callback = event_callback
        self.foreground_timeout_s = min(foreground_timeout_s, FOREGROUND_LIMIT_S)
        self.background_timeout_s = min(background_timeout_s, BACKGROUND_LIMIT_S)
        self.max_return_chars = max_return_chars
        self.agent_id = agent_id
        self.last_result: CodeRunResult | None = None
        self._jobs: dict[str, CodeRunResult] = {}

    # ---- helpers ------------------------------------------------------------
    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return (self.worktree_root / p).resolve() if not p.is_absolute() else p.resolve()

    def rel(self, path: str | Path) -> str:
        r = self.resolve(path)
        return str(r.relative_to(self.worktree_root)) if r.is_relative_to(self.worktree_root) else str(r)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback({"agent_id": self.agent_id, "event_type": event_type, **payload})

    # ---- STEP 0 -------------------------------------------------------------
    def review_code(self, code: str) -> CodeReview:
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            return CodeReview(allowed=False, findings=[f"SyntaxError: {exc.msg} (line {exc.lineno})"])
        reviewer = _CodeReviewer()
        reviewer.visit(tree)
        return CodeReview(allowed=not reviewer.findings, findings=reviewer.findings)

    async def run_hooks(self, tool: str, tool_input: dict[str, Any]) -> tuple[bool, str, str]:
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
        for hook in self.hooks:
            if not re.fullmatch(hook.matcher, tool):
                continue
            try:
                out = await asyncio.wait_for(asyncio.to_thread(hook.fn, payload), timeout=hook.timeout_s)
            except (TimeoutError, asyncio.TimeoutError):
                if hook.on_failure == "continue":
                    continue
                return False, f"hook:{hook.name}", "the hook did not finish (timeout) — raise the hook's timeout"
            except Exception as exc:  # noqa: BLE001
                if hook.on_failure == "continue":
                    continue
                return False, f"hook:{hook.name}", f"the hook failed ({type(exc).__name__}: {exc})"
            if out and out.get("decision") == "deny":
                return False, f"hook:{hook.name}", out.get("reason", "denied by hook")
        return True, "", ""

    # ---- 코드에 노출되는 Tool API -------------------------------------------
    def _namespace(self, ctx: _RunContext) -> dict[str, Any]:
        ex = self

        def _env() -> dict[str, str]:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ex.worktree_root)
            env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"
            return env

        async def sh(command: str, cwd: str | None = None, nothrow: bool = False) -> ShellResult:
            def impl() -> ShellResult:
                run_cwd = ex.resolve(cwd) if cwd else ex.worktree_root
                try:
                    proc = subprocess.run(
                        command, shell=True, cwd=str(run_cwd), env=_env(),
                        capture_output=True, text=True, timeout=ctx.remaining(),
                    )
                    return ShellResult(command, proc.returncode, proc.stdout, proc.stderr)
                except subprocess.TimeoutExpired:
                    return ShellResult(command, 124, "", "timed out (code budget)")

            args = {"command": command, **({"cwd": cwd} if cwd else {})}
            res: ShellResult = await ctx.call("Bash", args, impl)
            if not res.ok and not nothrow:
                raise CommandFailed(res)
            return res

        async def read(path: str) -> str:
            def impl() -> str:
                return ex.resolve(path).read_text(encoding="utf-8", errors="replace")
            return await ctx.call("Read", {"path": path}, impl)

        async def ls(path: str = ".", pattern: str = "**/*") -> list[str]:
            def impl() -> list[str]:
                base = ex.resolve(path)
                return sorted(
                    str(p.relative_to(ex.worktree_root)) for p in base.glob(pattern)
                    if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
                )[:500]
            return await ctx.call("Glob", {"path": path, "pattern": pattern}, impl)

        async def write(path: str, content: str) -> str:
            def impl() -> tuple[str, str]:
                target = ex.resolve(path)
                before = target.read_text(encoding="utf-8") if target.exists() else ""
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return f"wrote {len(content)} bytes to {path}", _diff(path, before, content)
            return await ctx.call("Write", {"path": path, "content_chars": len(content)}, impl)

        async def edit(path: str, old: str, new: str) -> str:
            def impl() -> tuple[str, str]:
                target = ex.resolve(path)
                before = target.read_text(encoding="utf-8")
                if old not in before:
                    raise ValueError(f"edit target text not found in {path}: {old!r}")
                after = before.replace(old, new, 1)
                target.write_text(after, encoding="utf-8")
                return f"edited {path}", _diff(path, before, after)
            return await ctx.call("Edit", {"path": path, "old": old, "new": new}, impl)

        async def gather(*aws: Any) -> list[Any]:
            return list(await asyncio.gather(*aws))

        def _print(*parts: Any) -> None:
            ctx.result.logs.append(" ".join(str(p) for p in parts))

        def _range(*a: int) -> range:
            r = range(*a)
            if len(r) > MAX_RANGE:
                raise BudgetExceeded(f"range() larger than {MAX_RANGE} is not allowed")
            return r

        safe = {
            n: getattr(builtins, n)
            for n in (
                "len", "sorted", "min", "max", "sum", "any", "all", "abs", "round", "enumerate",
                "zip", "map", "filter", "reversed", "list", "dict", "set", "tuple", "str", "int",
                "float", "bool", "isinstance", "repr", "Exception", "ValueError", "KeyError",
                "RuntimeError", "TimeoutError", "StopIteration",
            )
        }
        safe.update(print=_print, range=_range)
        return {
            "__builtins__": safe,
            "__abyss_tick__": ctx.tick,
            "sh": sh, "read": read, "ls": ls, "write": write, "edit": edit, "gather": gather,
            "to_json": lambda v: json.dumps(v, ensure_ascii=False, default=str),
            "from_json": json.loads,
            "ToolRefused": ToolRefused, "CommandFailed": CommandFailed,
        }

    # ---- 반환값 렌더링: "모델이 읽어야 할 부분"만 ------------------------------
    def _jsonable(self, value: Any) -> Any:
        if isinstance(value, ShellResult):
            return value.to_dict()
        if isinstance(value, dict):
            return {str(k): self._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._jsonable(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return repr(value)

    def _render(self, value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        if len(text) > self.max_return_chars:
            cut = len(text) - self.max_return_chars
            text = text[: self.max_return_chars] + f"\n...[truncated {cut} chars — return less]"
        return text

    # ---- STEP 1~3: 실행 ------------------------------------------------------
    async def arun(self, code: str, *, background: bool = False, result: CodeRunResult | None = None) -> CodeRunResult:
        result = result or CodeRunResult(run_id=uuid.uuid4().hex[:10], code=code, background=background)
        t0 = time.monotonic()
        self.emit("code_mode_started", {"run_id": result.run_id, "background": background, "code_lines": code.count("\n") + 1})

        # STEP 0: REPL PreToolUse (정적 검토 + REPL 매칭 훅)
        result.review = self.review_code(code)
        if result.review.allowed:
            ok, who, why = await self.run_hooks(REPL_TOOL_NAME, {"code": code})
            if not ok:
                result.review = CodeReview(allowed=False, findings=[f"{who}: {why}"])
        if not result.review.allowed:
            result.status, result.error = "refused_by_review", "REPL PreToolUse review refused this code."
            return self._finish(result, t0)

        # STEP 1: compile
        limit = self.background_timeout_s if background else self.foreground_timeout_s
        ctx = _RunContext(self, result, limit)
        ns = self._namespace(ctx)
        exec(compile_program(code), ns)  # noqa: S102 — defines __abyss_main__ only

        # STEP 2~3: 실행 (각 tool call은 ctx.call → gate)
        try:
            value = await asyncio.wait_for(ns["__abyss_main__"](), timeout=limit)
            result.return_value = self._jsonable(value)
            result.return_text = self._render(result.return_value)
            result.status = "ok"
        except (TimeoutError, asyncio.TimeoutError, BudgetExceeded) as exc:
            result.status = "timeout"
            result.error = f"Code stopped: {exc or 'time limit'} (limit {int(limit)}s; foreground max 10 min, background max 2 h)."
        except (ToolRefused, CommandFailed) as exc:
            result.status, result.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001
            result.status, result.error = "error", f"{type(exc).__name__}: {exc}"
        return self._finish(result, t0)

    def _finish(self, result: CodeRunResult, t0: float) -> CodeRunResult:
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        self.last_result = result
        self.emit("code_mode_finished", {"run_id": result.run_id, "status": result.status, "tool_calls": len(result.tool_calls), "duration_ms": result.duration_ms})
        return result

    def run(self, code: str) -> CodeRunResult:
        """동기 진입점. 이미 이벤트 루프 안이면 별도 스레드에서 실행합니다."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(code))
        box: dict[str, CodeRunResult] = {}
        t = threading.Thread(target=lambda: box.setdefault("r", asyncio.run(self.arun(code))))
        t.start()
        t.join()
        return box["r"]

    # ---- STEP 4: background (최대 2시간) --------------------------------------
    def start_background(self, code: str) -> str:
        result = CodeRunResult(run_id=uuid.uuid4().hex[:10], code=code, background=True)
        self._jobs[result.run_id] = result
        threading.Thread(target=lambda: asyncio.run(self.arun(code, background=True, result=result)), daemon=True).start()
        return result.run_id

    def poll(self, run_id: str) -> CodeRunResult | None:
        return self._jobs.get(run_id)

    # ---- AgentRuntime 연동용 tool 스키마 -------------------------------------
    def tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": REPL_TOOL_NAME,
                "description": CODE_MODE_TOOL_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string", "description": "Async Python snippet. The value of the last expression is returned to you."}},
                    "required": ["code"],
                },
            },
        }


def _diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}", n=2)
    )[:4000]


CODE_MODE_TOOL_DESCRIPTION = """Run an async Python snippet that makes several tool calls in one step (Code Mode).
Available (all async, use `await`):
  sh(command, cwd=None, nothrow=False) -> ShellResult(.text, .lines(), .tail(n), .ok, .exit_code)
      raises CommandFailed on non-zero exit unless nothrow=True
  read(path) -> str        ls(path=".", pattern="**/*") -> list[str]
  write(path, content)     edit(path, old, new)
  gather(*awaitables) -> list   # run independent calls in parallel
Helpers: to_json, from_json, print (goes to logs). No imports.
Every call is checked by permission rules, worktree isolation and PreToolUse hooks; a refused call raises ToolRefused.
End with an expression containing ONLY what you need to read (e.g. a small dict) - it is returned to you."""
