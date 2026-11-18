from __future__ import annotations

import time
from pathlib import Path

from abyss.core.agent_runtime import AgentRuntime
from abyss.core.code_mode import CodeModeExecutor, HookSpec
from abyss.core.permissions import PermissionRules, split_compound_command


def _setup_sample_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main_root = tmp_path / "main_repo"
    wt_root = main_root / ".abyss" / "worktrees" / "worker-code"
    (wt_root / "src").mkdir(parents=True)
    (wt_root / "docs").mkdir(parents=True)

    (wt_root / "src" / "retry_policy.py").write_text(
        '"""Retry helper."""\n'
        "def retryPolicy() -> dict:\n"
        '    return {"maxAttempts": 3, "backoffMs": 100}\n',
        encoding="utf-8",
    )
    (wt_root / "src" / "test_retry.py").write_text(
        "from src.retry_policy import retryPolicy\n\n"
        "def test_retry_limit() -> None:\n"
        '    assert retryPolicy()["maxAttempts"] == 5\n',
        encoding="utf-8",
    )
    (wt_root / "docs" / "guide.md").write_text(
        "# Guide\n"
        "See [retry_policy](../src/retry_policy.py) and [missing](./does_not_exist.md).\n",
        encoding="utf-8",
    )
    return main_root, wt_root


def test_code_mode_doc_example_find_edit_test_in_one_step(tmp_path: Path) -> None:
    """
    Claude Code 문서 예제 검증:
      const files = await $`rg -l "function retryPolicy" src`.text();
      const target = files.trim().split("\\n")[0];
      await edit(target, "maxAttempts: 3", "maxAttempts: 5");
      ({ edited: target, tests: $`npm test -- retryPolicy`.nothrow() });
    """
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    code = """
files = (await sh('grep -rl "def retryPolicy" src')).text
target = files.strip().splitlines()[0]
await edit(target, '"maxAttempts": 3', '"maxAttempts": 5')
tests = await sh("pytest -q src/test_retry.py", nothrow=True)
{"edited": target, "tests": {"exit_code": tests.exit_code, "passed": tests.ok, "summary": tests.tail(3)}}
"""
    res = executor.run(code)

    assert res.status == "ok"
    assert res.return_value["edited"] == "src/retry_policy.py"
    assert res.return_value["tests"]["passed"] is True
    assert len(res.tool_calls) == 3
    assert [c.tool for c in res.tool_calls] == ["Bash", "Edit", "Bash"]
    # Edit 호출에는 unified diff가 자동 기록되어야 함 (대화창에 코드 대신 diff 표시용)
    assert '-    return {"maxAttempts": 3' in res.tool_calls[1].diff
    assert '+    return {"maxAttempts": 5' in res.tool_calls[1].diff
    assert res.files_touched == ["src/retry_policy.py"]


def test_code_mode_parallel_gather_and_markdown_link_audit(tmp_path: Path) -> None:
    """문서의 Try Code Mode 예제: 모든 Markdown 파일을 읽어 깨진 로컬 링크만 추려서 반환."""
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    code = """
md_files = await ls(".", "**/*.md")
contents = await gather(*[read(p) for p in md_files])
existing = set(await ls(".", "**/*"))
broken = []
for md_path, text in zip(md_files, contents):
    for chunk in text.split("](")[1:]:
        link = chunk.split(")")[0]
        if link.startswith("./") or link.startswith("../"):
            norm = link.replace("../", "").replace("./", "docs/")
            if norm not in existing:
                broken.append({"file": md_path, "missing_link": link})
{"scanned_md": len(md_files), "broken_links": broken}
"""
    res = executor.run(code)
    assert res.status == "ok"
    assert res.return_value["scanned_md"] == 1
    assert res.return_value["broken_links"] == [
        {"file": "docs/guide.md", "missing_link": "./does_not_exist.md"}
    ]


def test_compound_command_permission_blocks_cd_and_git_push(tmp_path: Path) -> None:
    """
    문서 경고 검증:
      "In Code Mode, a `git push` in a folder named `app` can reach your hook as `cd 'app' && git push`."
    세그먼트 분리를 통해 `Bash(git push*)` deny 규칙이 정확히 차단해야 함.
    """
    segments = split_compound_command("cd 'app' && FOO=1 git push origin main 2>&1; echo done")
    assert segments == ["cd 'app'", "FOO=1 git push origin main 2>&1", "echo done"]

    main_root, wt_root = _setup_sample_worktree(tmp_path)
    (wt_root / "app").mkdir()
    executor = CodeModeExecutor(
        worktree_root=wt_root,
        main_checkout_root=main_root,
        permissions=PermissionRules.default(),
    )

    res = executor.run("await sh(\"cd 'app' && git push origin main\")")
    assert res.status == "error"
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].allowed is False
    assert res.tool_calls[0].decided_by == "permission:deny"
    assert "Bash(git push*)" in res.tool_calls[0].reason


def test_runtime_computed_path_escape_blocked_by_per_call_isolation(tmp_path: Path) -> None:
    """
    문서 Auto mode 한계 검증:
      "The classifier reviews each piece of code before it runs, not each tool call.
       It doesn't see values computed at run time, such as a path taken from search output."
    코드 정적 검토(REPL review)는 통과하더라도 런타임에 조합된 상위 경로(`../../secret.txt`)는
    내부 Write 호출 시점의 WorktreeIsolationPolicy에서 차단되어야 함.
    """
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    code = """
parts = ["..", "..", "escaped_in_main.py"]
dynamic_target = "/".join(parts)
await write(dynamic_target, "pwned")
"""
    res = executor.run(code)
    assert res.review is not None and res.review.allowed is True  # 정적 분석은 통과
    assert res.status == "error"
    assert res.tool_calls[0].allowed is False
    assert res.tool_calls[0].decided_by == "isolation:file_edit"
    assert not (main_root / "escaped_in_main.py").exists()


def test_pre_tool_use_hook_timeout_and_failure_semantics(tmp_path: Path) -> None:
    """
    문서 Troubleshooting 검증:
      - `the hook did not finish`: PreToolUse 훅 타임아웃 시 호출 거부
      - `the hook failed`: 훅 예외 발생 시 호출 거부
      - `onFailure: "continue"`: 타임아웃/실패 시에도 호출 진행 허용
    """
    main_root, wt_root = _setup_sample_worktree(tmp_path)

    def slow_hook(_payload: dict) -> dict | None:
        time.sleep(0.25)
        return None

    # 1) 기본 on_failure="block" → 타임아웃 시 거부
    exec_block = CodeModeExecutor(
        worktree_root=wt_root,
        main_checkout_root=main_root,
        hooks=[HookSpec(name="sec-audit", matcher="Bash", fn=slow_hook, timeout_s=0.05, on_failure="block")],
    )
    res_blocked = exec_block.run('await sh("echo hello")')
    assert res_blocked.status == "error"
    assert "the hook did not finish" in res_blocked.error

    # 2) on_failure="continue" → 타임아웃이어도 통과
    exec_cont = CodeModeExecutor(
        worktree_root=wt_root,
        main_checkout_root=main_root,
        hooks=[HookSpec(name="sec-audit", matcher="Bash", fn=slow_hook, timeout_s=0.05, on_failure="continue")],
    )
    res_ok = exec_cont.run('(await sh("echo hello")).text.strip()')
    assert res_ok.status == "ok"
    assert res_ok.return_value == "hello"


def test_repl_static_review_blocks_import_and_dunder(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    res = executor.run("import os\nos.system('id')")
    assert res.status == "refused_by_review"
    assert res.review is not None and not res.review.allowed
    assert any("import is not allowed" in f for f in res.review.findings)


def test_agent_runtime_with_code_mode_completes_in_single_tool_turn(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    cm = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    runtime = AgentRuntime(
        agent_id="worker-code",
        role="worker",
        worktree_root=wt_root,
        main_checkout_root=main_root,
        worktree_branch="worktree-worker-code",
        tools_registry={},
        tools_schema=[],
        code_mode=cm,
    )

    step_counter = 0

    def fake_llm(_messages: list[dict], tools_schema: list[dict]) -> dict:
        nonlocal step_counter
        step_counter += 1
        assert len(tools_schema) == 1
        assert tools_schema[0]["function"]["name"] == "REPL"
        if step_counter == 1:
            return {
                "provider": "qwen",
                "content": "<think>Code Mode REPL로 검색·수정·테스트를 한 스텝에 수행합니다.</think>",
                "tool_calls": [
                    {
                        "id": "call_repl_1",
                        "function": {
                            "name": "REPL",
                            "arguments": {
                                "code": (
                                    'files = (await sh(\'grep -rl "def retryPolicy" src\')).lines()\n'
                                    "target = files[0]\n"
                                    'await edit(target, \'"maxAttempts": 3\', \'"maxAttempts": 5\')\n'
                                    't = await sh("pytest -q src/test_retry.py", nothrow=True)\n'
                                    '{"edited": target, "ok": t.ok}'
                                )
                            },
                        },
                    }
                ],
            }
        return {
            "provider": "qwen",
            "content": "Done in 1 REPL turn.",
            "tool_calls": [],
        }

    artifact = runtime.run(
        system_prompt="You are a Code Mode worker.",
        task_prompt="Raise retryPolicy maxAttempts from 3 to 5 and run tests.",
        llm_callable=fake_llm,
    )

    assert len(artifact.tool_history) == 1
    assert artifact.tool_history[0].tool_name == "REPL"
    assert artifact.files_touched == ["src/retry_policy.py"]
