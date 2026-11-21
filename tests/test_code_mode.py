"""
Unit tests for Focus Domain #2 (`app/domains/execution/{code_mode,runtime}.py`)
and Focus Domain #1 (`app/domains/guardrails/permissions.py`).
"""

from __future__ import annotations

import time
from pathlib import Path

from app.domains.execution.code_mode import CodeModeExecutor, HookSpec
from app.domains.execution.deepagent_backend import GuardedWorktreeBackend
from app.domains.execution.runtime import DeepAgentRuntime
from app.domains.guardrails.permissions import PermissionRules, split_compound_command
from tests.helpers_llm_doubles import ScriptedDeepAgentChatModel, ai_text, ai_tool_call


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
    assert '-    return {"maxAttempts": 3' in res.tool_calls[1].diff
    assert '+    return {"maxAttempts": 5' in res.tool_calls[1].diff
    assert res.files_touched == ["src/retry_policy.py"]


def test_code_mode_parallel_gather_and_markdown_link_audit(tmp_path: Path) -> None:
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
    assert res.status == "refused"
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].allowed is False
    assert res.tool_calls[0].decided_by == "permission:deny"
    assert "Bash(git push*)" in res.tool_calls[0].reason


def test_runtime_computed_path_escape_blocked_by_per_call_isolation(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    code = """
parts = ["..", "..", ".env"]
dynamic_target = "/".join(parts)
await write(dynamic_target, "LEAKED=1")
"""
    res = executor.run(code)
    assert res.review is not None and res.review.allowed is True
    assert res.status == "refused"
    assert res.tool_calls[0].allowed is False
    assert res.tool_calls[0].decided_by == "isolation:file_edit"
    assert not (main_root / ".env").exists()


def test_command_failed_still_reports_error_status(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    res = executor.run('await sh("exit 2")')
    assert res.status == "error"
    assert "exit 2" in res.error


def test_pre_tool_use_hook_timeout_and_failure_semantics(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)

    def slow_hook(_payload: dict) -> dict | None:
        time.sleep(0.25)
        return None

    exec_block = CodeModeExecutor(
        worktree_root=wt_root,
        main_checkout_root=main_root,
        hooks=[HookSpec(name="sec-audit", matcher="Bash", fn=slow_hook, timeout_s=0.05, on_failure="block")],
    )
    res_blocked = exec_block.run('await sh("echo hello")')
    assert res_blocked.status == "refused"
    assert "the hook did not finish" in res_blocked.error

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


def test_deepagent_runtime_with_code_mode_repl_tool(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    cm = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)
    backend = GuardedWorktreeBackend(worktree_root=wt_root, main_checkout_root=main_root)

    model = ScriptedDeepAgentChatModel(
        steps=[
            ai_tool_call(
                "run_python",
                {
                    "code": (
                        'files = (await sh(\'grep -rl "def retryPolicy" src\')).lines()\n'
                        "target = files[0]\n"
                        'await edit(target, \'"maxAttempts": 3\', \'"maxAttempts": 5\')\n'
                        't = await sh("pytest -q src/test_retry.py", nothrow=True)\n'
                        '{"edited": target, "ok": t.ok}'
                    )
                },
                content="<think>Code Mode REPL로 검색·수정·테스트를 한 스텝에 수행합니다.</think>",
            ),
            ai_text("Done in 1 REPL turn."),
        ]
    )

    runtime = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="repl",
        code_mode=cm,
        workflow="fast",
        completion_cmd=None,
        rubric_enabled=False,
    )
    res = runtime.run(
        system_prompt="You are a Code Mode worker.",
        task_prompt="Raise retryPolicy maxAttempts from 3 to 5 and run tests.",
    )

    assert res.passed is True
    assert any(t.tool_name == "run_python" for t in res.turn_trace)
    assert res.files_touched == ["src/retry_policy.py"]
    assert '"maxAttempts": 5' in (wt_root / "src" / "retry_policy.py").read_text(encoding="utf-8")


def test_deepagent_runtime_default_workflow_and_compaction_support(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    backend = GuardedWorktreeBackend(worktree_root=wt_root, main_checkout_root=main_root)
    model = ScriptedDeepAgentChatModel(steps=[ai_text("Summary complete.")])

    builder_rt = DeepAgentRuntime(model=model, backend=backend, mode="builder", completion_cmd=None, rubric_enabled=False)
    assert builder_rt.workflow == "planning"
    assert builder_rt.plan_review in ("request_review", "always_proceed")

    runtime = DeepAgentRuntime(
        model=model,
        backend=backend,
        mode="direct",
        completion_cmd=None,
        rubric_enabled=False,
    )
    assert runtime.workflow == "fast"
    res = runtime.run("System", "Summarize repo.")
    assert res.passed is True
    assert res.summary == "Summary complete."


def test_code_mode_blocks_str_format_globals_secret_exfiltration(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("INTERNAL_GITLAB_TOKEN", "CANARY_LEAK_98765")
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    res1 = executor.run('"{0.__globals__[os].environ}".format(sh)')
    assert res1.status == "refused_by_review"
    assert "CANARY_LEAK_98765" not in res1.model_view()

    res2 = executor.run('"{x.__globals__}".format_map({"x": sh})')
    assert res2.status == "refused_by_review"
    assert "CANARY_LEAK_98765" not in res2.model_view()


def test_code_mode_scrubs_secret_env_vars_in_subprocess_sh(
    tmp_path: Path, monkeypatch
) -> None:
    canaries = {
        "INTERNAL_GITLAB_TOKEN": "CANARY_GITLAB_11111",
        "QWEN_API_KEY": "CANARY_QWEN_22222",
        "GEMINI_API_KEY": "CANARY_GEMINI_33333",
        "OPENAI_API_KEY": "CANARY_OPENAI_44444",
    }
    for k, v in canaries.items():
        monkeypatch.setenv(k, v)

    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    res = executor.run('(await sh("env")).text')
    assert res.status == "ok"
    text_out = str(res.return_value or "")
    view_out = res.model_view()
    for val in canaries.values():
        assert val not in text_out
        assert val not in view_out


def test_code_mode_rejects_empty_or_separator_only_command_and_enforces_cwd(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    for snippet in (
        'await sh("   ", cwd="/etc", nothrow=True)',
        'await sh(";", cwd="..", nothrow=True)',
    ):
        res = executor.run(snippet)
        assert res.status == "refused", f"Expected refused for {snippet!r}, got {res.status}"
        assert len(res.tool_calls) == 1
        assert res.tool_calls[0].allowed is False


def test_code_mode_blocks_general_command_substitution_in_sh(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(worktree_root=wt_root, main_checkout_root=main_root)

    for cmd in ("echo $(id)", "echo $(git push --help)", "$(echo git) status"):
        res = executor.run(f"await sh({cmd!r})")
        assert res.status == "refused", f"Expected refused for {cmd!r}, got {res.status}"
        assert res.tool_calls[0].decided_by == "isolation:command_shape"


def test_code_mode_enforces_loop_budget_inside_nested_comprehensions(tmp_path: Path) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(
        worktree_root=wt_root,
        main_checkout_root=main_root,
        foreground_timeout_s=2.0,
    )

    res = executor.run("sum(1 for _ in range(1000) for _ in range(1000))")
    assert res.status == "timeout"
    assert "budget" in res.error.lower() or "timed out" in res.error.lower()


def test_guarded_backend_enforces_read_confinement_path_norm_and_ask_gate(
    tmp_path: Path,
) -> None:
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    backend = GuardedWorktreeBackend(
        worktree_root=wt_root,
        main_checkout_root=main_root,
    )

    assert backend.read("../../../.env").error is not None
    assert backend.read("/.env").error is not None
    assert backend.ls("..").error is not None
    assert backend.write("sub/../.env", "x").error is not None
    exec_res = backend.execute("pip install evilpkg")
    assert exec_res.exit_code != 0
    assert "[WORKTREE ISOLATION REFUSAL" in exec_res.output
    assert len(backend.blocked_attempts) >= 5


def test_code_mode_blocks_match_class_coroutine_frames_lambda_spin_and_cloud_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "CANARY_AWS_55555")
    monkeypatch.setenv("DATABASE_PASSWORD", "CANARY_DB_66666")
    main_root, wt_root = _setup_sample_worktree(tmp_path)
    executor = CodeModeExecutor(
        worktree_root=wt_root,
        main_checkout_root=main_root,
        foreground_timeout_s=2.0,
    )

    # 1. ast.MatchClass attribute extraction blocked
    res_match = executor.run(
        "match sh:\n    case object(__globals__=g):\n        g\n"
    )
    assert res_match.status == "refused_by_review"

    # 2. Coroutine frame inspection via tool proxy blocked
    res_coro = executor.run("c = read('src/retry_policy.py')\nc.cr_frame")
    assert res_coro.status == "refused_by_review"

    # 3. Lambda recursion / tight budget caught
    res_lam = executor.run("f = lambda n: 1 if n <= 0 else f(n - 1)\nf(500000)")
    assert res_lam.status in ("timeout", "error")

    # 4. Expanded sensitive env vars scrubbed from subprocess sh()
    res_env = executor.run('(await sh("env")).text')
    assert res_env.status == "ok"
    assert "CANARY_AWS_55555" not in str(res_env.return_value)
    assert "CANARY_DB_66666" not in str(res_env.return_value)

