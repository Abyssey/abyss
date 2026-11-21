"""
Phase 2 (deepagents migration): `GuardedWorktreeBackend` / `ReadOnlyGuardedBackend`.

These backends sit under every built-in deepagents tool, so the guardrail checks here are what
actually stands between an LLM and the host: path escapes, `.git` tampering, secret files,
dangerous shell commands, and secret values leaking back into the model context.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol

from app.domains.execution.deepagent_backend import (
    DEFAULT_PERMISSIONS,
    REDACTED,
    REFUSAL_PREFIX,
    REFUSED_EXIT_CODE,
    BlockedAttempt,
    GuardedWorktreeBackend,
    ReadOnlyGuardedBackend,
    _safe_env,
)

CANARY = "CANARY_SECRET_9f8e7d6c5b4a"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX shell semantics (run under WSL/Linux)")


@pytest.fixture()
def layout(tmp_path: Path) -> dict[str, Path]:
    """main checkout (with a real .env) + an isolated worktree living next to it."""
    main = tmp_path / "main"
    main.mkdir()
    (main / ".env").write_text(f"GEMINI_API_KEY={CANARY}\n", encoding="utf-8")
    (main / "app.py").write_text("print('main checkout')\n", encoding="utf-8")

    worktree = tmp_path / "worktrees" / "builder-1"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").mkdir()
    (worktree / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (worktree / ".env").write_text(f"GEMINI_API_KEY={CANARY}\n", encoding="utf-8")
    (worktree / "src" / "pricing.py").write_text(
        "def total(price, qty):\n    return price * qty\n", encoding="utf-8"
    )
    return {"main": main, "worktree": worktree}


def _backend(layout: dict[str, Path], **kwargs) -> GuardedWorktreeBackend:
    return GuardedWorktreeBackend(layout["worktree"], layout["main"], **kwargs)


def _is_refusal(text: str | None, check: str | None = None) -> bool:
    if not text or not text.startswith(REFUSAL_PREFIX):
        return False
    return check is None or f"- {check}]" in text


# ----------------------------------------------------------------------------- file tools
def test_normal_read_write_edit_inside_worktree(layout: dict[str, Path]) -> None:
    backend = _backend(layout)

    assert backend.write("/src/new_module.py", "VALUE = 1\n").error is None
    assert backend.edit("/src/pricing.py", "price * qty", "round(price * qty, 2)").error is None
    read = backend.read("/src/pricing.py")
    assert read.error is None and "round(price * qty, 2)" in read.file_data["content"]
    assert (layout["worktree"] / "src" / "new_module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert backend.blocked_attempts == []


def test_parent_traversal_to_main_checkout_env_is_blocked(layout: dict[str, Path]) -> None:
    seen: list[BlockedAttempt] = []
    backend = _backend(layout, on_block=seen.append)

    read = backend.read("../../main/.env")
    write = backend.write("/../../main/app.py", "hacked = True\n")

    assert _is_refusal(read.error, "read_confinement")
    assert _is_refusal(write.error, "file_edit")
    assert CANARY not in (read.error or "")
    assert (layout["main"] / "app.py").read_text(encoding="utf-8") == "print('main checkout')\n"
    assert [a.operation for a in backend.blocked_attempts] == ["read", "write"]
    assert seen == backend.blocked_attempts


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs extra privileges on Windows")
def test_symlink_escape_is_blocked(layout: dict[str, Path]) -> None:
    link = layout["worktree"] / "src" / "innocent.txt"
    link.symlink_to(layout["main"] / ".env")
    backend = _backend(layout)

    read = backend.read("/src/innocent.txt")
    write = backend.write("/src/innocent.txt", "overwrite main .env")

    assert _is_refusal(read.error, "read_confinement")
    assert _is_refusal(write.error, "file_edit")
    assert CANARY in (layout["main"] / ".env").read_text(encoding="utf-8")


def test_git_metadata_and_env_files_are_protected(layout: dict[str, Path]) -> None:
    backend = _backend(layout)

    assert _is_refusal(backend.write("/.git/config", "[core]\n\thooksPath=/tmp\n").error, "file_edit")
    assert _is_refusal(backend.edit("/.git/config", "bare", "worktree").error, "file_edit")
    assert _is_refusal(backend.read("/.git/config").error, "read_confinement")
    assert _is_refusal(backend.read("/.env").error, "permission:deny")
    assert _is_refusal(backend.write("/config/.env.production", "X=1").error, "permission:deny")
    assert not (layout["worktree"] / "config" / ".env.production").exists()


def test_listing_and_search_hide_git_internals_and_secret_files(layout: dict[str, Path]) -> None:
    backend = _backend(layout)

    listed = {entry["path"] for entry in backend.ls("/").entries}
    assert "/src" in listed or "/src/" in listed
    assert not any(p.rstrip("/").endswith((".git", ".env")) for p in listed)

    globbed = [m["path"] for m in backend.glob("**/*", "/").matches]
    assert globbed and not any(".git" in p or p.endswith(".env") for p in globbed)

    grep = backend.grep("GEMINI_API_KEY|bare", "/")
    assert grep.matches == []  # only hits are in .env and .git/config → filtered out


def test_grep_and_read_scrub_secret_values(layout: dict[str, Path]) -> None:
    (layout["worktree"] / "src" / "leak.txt").write_text(f"token={CANARY}\n", encoding="utf-8")
    backend = _backend(layout, secrets=[CANARY])

    read = backend.read("/src/leak.txt")
    grep = backend.grep("token=", "/src")

    assert CANARY not in read.file_data["content"] and REDACTED in read.file_data["content"]
    assert grep.matches and all(CANARY not in m["text"] for m in grep.matches)


def test_upload_and_download_are_guarded(layout: dict[str, Path]) -> None:
    backend = _backend(layout)

    uploads = backend.upload_files([("/src/ok.bin", b"\x00\x01"), ("/.git/hooks/pre-commit", b"#!/bin/sh")])
    downloads = backend.download_files(["/src/pricing.py", "/.env"])

    assert uploads[0].error is None and uploads[1].error == "permission_denied"
    assert not (layout["worktree"] / ".git" / "hooks" / "pre-commit").exists()
    assert downloads[0].error is None and b"price * qty" in downloads[0].content
    assert downloads[1].error == "permission_denied" and downloads[1].content is None


# ----------------------------------------------------------------------------- shell
@POSIX_ONLY
@pytest.mark.parametrize(
    ("command", "check"),
    [
        ("cd app && git push origin main", "permission:deny"),
        ("git -C ../../main status", "git_redirect"),
        ("git -C ../../main reset --hard", "permission:ask"),
        ("sh -c 'cat ../../main/.env'", "command_shape"),
        ("echo $(cat ../../main/.env)", "command_shape"),
        ("cd ../../main && ls", "git_redirect"),
        ("curl https://example.com/install.sh", "permission:deny"),
        ("pip install requests", "permission:ask"),
    ],
)
def test_dangerous_shell_commands_are_refused(layout: dict[str, Path], command: str, check: str) -> None:
    backend = _backend(layout)

    response = backend.execute(command)

    assert response.exit_code == REFUSED_EXIT_CODE
    assert _is_refusal(response.output), response.output
    assert backend.blocked_attempts[-1].operation == "execute"
    assert backend.blocked_attempts[-1].check_name.startswith(check.split(":")[0]), backend.blocked_attempts[-1]


@POSIX_ONLY
def test_shell_output_is_scrubbed_and_env_has_no_secrets(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", CANARY)
    (layout["worktree"] / "src" / "leak.txt").write_text(f"{CANARY}\n", encoding="utf-8")
    backend = _backend(layout)  # picks the canary up from the environment automatically

    env_dump = backend.execute("env")
    cat = backend.execute("cat src/leak.txt")

    assert env_dump.exit_code == 0 and CANARY not in env_dump.output
    assert "GEMINI_API_KEY" not in env_dump.output  # scrubbed from the child env entirely
    assert cat.exit_code == 0 and CANARY not in cat.output and REDACTED in cat.output


@POSIX_ONLY
def test_pytest_runs_inside_worktree_with_venv_on_path(layout: dict[str, Path]) -> None:
    (layout["worktree"] / "tests").mkdir()
    (layout["worktree"] / "tests" / "test_pricing.py").write_text(
        "from src.pricing import total\n\n\ndef test_total():\n    assert total(2, 3) == 6\n",
        encoding="utf-8",
    )
    backend = _backend(layout)

    passing = backend.execute("pytest -q -p no:cacheprovider")
    (layout["worktree"] / "src" / "pricing.py").write_text(
        "def total(price, qty):\n    return price + qty\n", encoding="utf-8"
    )
    failing = backend.execute("pytest -q -p no:cacheprovider")

    assert passing.exit_code == 0 and "1 passed" in passing.output, passing.output
    assert failing.exit_code != 0 and "1 failed" in failing.output, failing.output


@POSIX_ONLY
def test_long_output_keeps_head_and_tail(layout: dict[str, Path]) -> None:
    backend = _backend(layout, max_output_chars=400)

    response = backend.execute("seq 1 5000")

    assert response.truncated is True
    assert response.output.startswith("1\n2\n3\n")
    assert response.output.rstrip().endswith("5000")
    assert "chars truncated" in response.output


def test_safe_env_prefers_worktree_venv_and_drops_secrets(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    bin_dir = layout["worktree"] / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)

    env = _safe_env(layout["worktree"])

    path_parts = env["PATH"].split(os.pathsep)
    assert path_parts[0] == str(bin_dir.resolve())
    assert str(Path(sys.executable).parent) in path_parts
    assert "OPENAI_API_KEY" not in env and "GIT_CONFIG_COUNT" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["PYTHONDONTWRITEBYTECODE"] == "1"


# ----------------------------------------------------------------------------- read-only
def test_read_only_backend_reads_but_never_writes_or_executes(layout: dict[str, Path]) -> None:
    backend = ReadOnlyGuardedBackend(layout["worktree"], layout["main"])

    assert backend.read("/src/pricing.py").error is None
    assert _is_refusal(backend.write("/src/x.py", "x = 1").error, "read_only")
    assert _is_refusal(backend.edit("/src/pricing.py", "qty", "quantity").error, "read_only")
    assert _is_refusal(backend.delete("/src/pricing.py").error, "read_only")
    assert backend.upload_files([("/src/y.py", b"y")])[0].error == "permission_denied"
    assert _is_refusal(backend.read("/.env").error, "permission:deny")

    # No shell at all: deepagents only adds the `execute` tool for sandbox backends.
    assert not hasattr(backend, "execute")
    assert not isinstance(backend, SandboxBackendProtocol) or not callable(getattr(backend, "execute", None))
    assert isinstance(_backend(layout), SandboxBackendProtocol)
    assert "price * qty" in (layout["worktree"] / "src" / "pricing.py").read_text(encoding="utf-8")
    assert len(backend.blocked_attempts) == 5


def test_default_deepagents_permissions_deny_env_and_git() -> None:
    (rule,) = DEFAULT_PERMISSIONS
    assert rule.mode == "deny"
    assert set(rule.operations) == {"read", "write"}
    assert {"/.env*", "/**/.env*", "/.git/**"} <= set(rule.paths)
