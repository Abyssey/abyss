"""
app/domains/execution/service.py
Use-Case Service for the Code Mode (`REPL`) & Guardrails Interactive Lab (Act 2 & Act 3).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Literal

from app.domains.execution.code_mode import CodeModeExecutor, HookSpec
from app.domains.guardrails.permissions import PermissionRules

CODE_MODE_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "retry_policy",
        "title": "1. [Act 2 · 1-Step REPL] 설정 파일 자동 찾아서 값 고치고 테스트까지 한 번에 끝내기",
        "badge": "1 Turn vs 4 Turns",
        "doc_section": "Code Mode REPL — Search → Edit → Pytest in 1 Step",
        "claude_js_code": (
            'const files = await $`rg -l "function retryPolicy" src`.text();\n'
            'const target = files.trim().split("\\n")[0];\n'
            'await edit(target, "maxAttempts: 3", "maxAttempts: 5");\n'
            '({ edited: target, tests: $`npm test -- retryPolicy`.nothrow() });'
        ),
        "python_code": (
            'files = (await sh(\'grep -rl "def retryPolicy" src\')).text\n'
            "target = files.strip().splitlines()[0]\n"
            'await edit(target, \'"maxAttempts": 3\', \'"maxAttempts": 5\')\n'
            'tests = await sh("pytest -q src/test_retry.py", nothrow=True)\n'
            '{"edited": target, "tests": {"exit_code": tests.exit_code, "passed": tests.ok, "output": tests.tail(4)}}'
        ),
        "learning_point": (
            "기존 방식에서는 ① 파일 검색 → ② AI가 결과 읽기 → ③ 값 수정 → ④ 테스트 실행까지 4번 왕복해야 합니다. "
            "Code Mode REPL은 단 1번의 실행으로 검색·수정·테스트를 끝내고 최종 요약 결과만 AI에게 돌려줍니다."
        ),
    },
    {
        "id": "broken_md_links",
        "title": "2. [Act 2 · 병렬 실행] 문서 파일 한꺼번에 읽고 깨진 링크만 골라내기",
        "badge": "Parallel + Filter",
        "doc_section": "Code Mode REPL — Parallel Read (`gather`) & In-Code Filtering",
        "claude_js_code": (
            "// Prompt: Read every Markdown file and return only broken local links\n"
            "const mdFiles = await glob('**/*.md');\n"
            "const texts = await Promise.all(mdFiles.map(f => read(f)));\n"
            "// Filter broken local links in code before returning to the model..."
        ),
        "python_code": (
            'md_files = await ls(".", "**/*.md")\n'
            "contents = await gather(*[read(p) for p in md_files])\n"
            'existing = set(await ls(".", "**/*"))\n'
            "broken = []\n"
            "for md_path, text in zip(md_files, contents):\n"
            '    for chunk in text.split("](")[1:]:\n'
            '        link = chunk.split(")")[0]\n'
            '        if link.startswith("./") or link.startswith("../"):\n'
            '            norm = link.replace("../", "").replace("./", "docs/")\n'
            "            if norm not in existing:\n"
            '                broken.append({"markdown": md_path, "broken_target": link})\n'
            '{"scanned_files": md_files, "broken_links_count": len(broken), "broken_links": broken}'
        ),
        "learning_point": (
            "여러 문서를 동시에 읽고(`gather`) 원문 전체를 모델에 넘기지 않은 채 코드 안에서 깨진 링크 2건만 추려 반환합니다."
        ),
    },
    {
        "id": "compound_cmd_trap",
        "title": "3. [Act 3 · 보안 가드레일] main 브랜치 push 우회 명령 차단 (cd 'app' && FOO=1 git push)",
        "badge": "Permission Gate",
        "doc_section": "Guardrails — Compound Shell Command Segment Splitting",
        "claude_js_code": (
            "// Guardrail check: a `git push` chained after `cd 'app' &&` must still be blocked.\n"
            "await $`cd 'app' && git push origin main`;"
        ),
        "python_code": (
            "# 복합 명령어 앞쪽에 cd나 환경변수를 붙여 Bash(git push*) 규칙 우회를 시도합니다.\n"
            "await sh(\"cd 'app' && FOO=1 git push origin main\")"
        ),
        "learning_point": (
            "단순 문자열 접두어 검사(`cmd.startswith('git push')`)는 `cd 'app' && git push`에 우회됩니다. "
            "app/domains/guardrails/permissions.py의 `split_compound_command()`가 `&&`, `||`, `;`, `|` 기준으로 세그먼트를 분리하고 "
            "환경변수를 정규화한 뒤 `Bash(git push*)` 규칙으로 결정론적 차단(`status: \"refused\"`)합니다."
        ),
    },
    {
        "id": "runtime_escape_and_hook",
        "title": "4. [Act 3 · 보안 가드레일] 상위 비밀 파일 탈출 쓰기 차단 (../../.env · 런타임 경로 조합)",
        "badge": "Runtime Isolation",
        "doc_section": "Guardrails — Runtime Computed Path Escape & PreToolUse Hook Timeout",
        "claude_js_code": (
            "// Static review cannot see paths computed at runtime; per-call isolation catches them.\n"
            "const dynamicPath = ['..', '..', '.env'].join('/');\n"
            "await write(dynamicPath, 'LEAKED=1');"
        ),
        "python_code": (
            "# 정적 AST 검사(STEP 0) 시점에는 문자열 리터럴이 보이지 않는 런타임 조합 경로\n"
            'parts = ["..", "..", ".env"]\n'
            'runtime_target = "/".join(parts)\n'
            'await write(runtime_target, "LEAKED=1")\n'
            '{"written": runtime_target}'
        ),
        "learning_point": (
            "코드 실행 전 정적 검토(STEP 0)는 런타임에 조합되는 경로(`../../.env`)를 알 수 없어 통과시키지만, "
            "STEP 2 내부 `Write` 실행 직전 `app/domains/guardrails/isolation_policy.py`가 실제 해석된 경로를 검증해 즉시 차단(`status: \"refused\"`)합니다."
        ),
    },
]


def reset_code_mode_sandbox(sandbox_root: Path) -> Path:
    """Code Mode(`REPL`) 실습용 격리 샌드박스 디렉토리를 초기화합니다."""
    (sandbox_root / "src").mkdir(parents=True, exist_ok=True)
    (sandbox_root / "docs").mkdir(parents=True, exist_ok=True)
    (sandbox_root / "app").mkdir(parents=True, exist_ok=True)

    (sandbox_root / "src" / "retry_policy.py").write_text(
        '"""Retry policy configuration for internal gateway calls."""\n\n'
        "def retryPolicy() -> dict:\n"
        '    return {"maxAttempts": 3, "backoffMs": 150, "jitter": True}\n',
        encoding="utf-8",
    )
    (sandbox_root / "src" / "test_retry.py").write_text(
        "from src.retry_policy import retryPolicy\n\n"
        "def test_retry_limit_raised_to_five() -> None:\n"
        "    cfg = retryPolicy()\n"
        "    assert cfg[\"maxAttempts\"] == 5, f\"Expected 5, got {cfg['maxAttempts']}\"\n",
        encoding="utf-8",
    )
    (sandbox_root / "docs" / "architecture.md").write_text(
        "# Architecture Docs\n"
        "- Valid link: [retry_policy.py](../src/retry_policy.py)\n"
        "- Broken link 1: [legacy_agent.py](../src/legacy_agent.py)\n",
        encoding="utf-8",
    )
    (sandbox_root / "docs" / "runbook.md").write_text(
        "# Internal Service Runbook\n"
        "- Valid link: [test_retry.py](../src/test_retry.py)\n"
        "- Broken link 2: [missing_config.yaml](./missing_config.yaml)\n",
        encoding="utf-8",
    )
    return sandbox_root


class CodeModeLabService:
    """Use-case service for executing Code Mode (`REPL`) lab snippets and metrics."""

    def __init__(
        self,
        sandbox_root: Path,
        main_checkout_root: Path,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.sandbox_root = Path(sandbox_root).resolve()
        self.main_checkout_root = Path(main_checkout_root).resolve()
        self.event_callback = event_callback

    def reset_sandbox(self) -> Path:
        return reset_code_mode_sandbox(self.sandbox_root)

    def get_scenarios_payload(self) -> dict[str, Any]:
        default_rules = PermissionRules.default()
        return {
            "env_flag": "CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1",
            "min_claude_code_version": "Code Mode (REPL) · Python Domain Implementation",
            "act3_prompt": "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘",
            "default_permissions": {
                "deny": default_rules.deny,
                "ask": default_rules.ask,
            },
            "scenarios": CODE_MODE_SCENARIOS,
        }

    async def run_snippet(
        self,
        code: str,
        scenario_id: str = "custom",
        reset_sandbox: bool = True,
        simulate_slow_hook: bool = False,
        hook_on_failure: Literal["block", "continue"] = "block",
    ) -> dict[str, Any]:
        """격리된 샌드박스 Worktree에서 Code Mode(`REPL`) 스니펫을 실행하고 효율 지표를 반환합니다."""
        if reset_sandbox or not self.sandbox_root.exists():
            self.reset_sandbox()

        hooks: list[HookSpec] = []
        if simulate_slow_hook:
            def _slow_pre_tool_hook(_payload: dict[str, Any]) -> dict[str, Any] | None:
                time.sleep(0.25)
                return None

            hooks.append(
                HookSpec(
                    name="slow-security-scanner",
                    matcher="Bash|Edit|Write",
                    fn=_slow_pre_tool_hook,
                    timeout_s=0.05,
                    on_failure=hook_on_failure,
                )
            )

        executor = CodeModeExecutor(
            worktree_root=self.sandbox_root,
            main_checkout_root=self.main_checkout_root,
            permissions=PermissionRules.default(),
            hooks=hooks,
            event_callback=self.event_callback,
            agent_id="code-mode-repl",
        )

        run_res = await executor.arun(code)
        res_dict = run_res.to_dict()

        inner_calls_count = max(1, len(run_res.tool_calls))
        raw_tool_chars = sum(len(c.output_preview) + len(c.diff) + 120 for c in run_res.tool_calls)
        returned_chars = max(1, len(run_res.return_text) or len(run_res.error))
        saved_pct = max(
            0,
            round((1.0 - min(1.0, returned_chars / max(raw_tool_chars, returned_chars + 1))) * 100),
        )

        return {
            "result": res_dict,
            "comparison": {
                "direct_mode_llm_turns": inner_calls_count + 1,
                "code_mode_llm_turns": 1,
                "inner_tool_calls_executed": len(run_res.tool_calls),
                "raw_intermediate_chars": raw_tool_chars,
                "returned_to_model_chars": returned_chars,
                "context_saved_pct": saved_pct,
            },
        }
