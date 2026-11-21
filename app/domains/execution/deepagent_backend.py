"""
app/domains/execution/deepagent_backend.py
deepagents `BackendProtocol` adapters that put the 4-Check worktree guardrails in front of
every built-in deepagents tool (ls / read_file / write_file / edit_file / glob / grep /
execute / upload / download / delete).

- `GuardedWorktreeBackend`  : builder / worker agents. Files + shell, confined to one git worktree.
- `ReadOnlyGuardedBackend`  : reviewer agents. Reads only; no `execute` (does not implement
                              `SandboxBackendProtocol`, so deepagents never exposes a shell tool).

Every refusal is
  1. recorded in `blocked_attempts` (and forwarded to `on_block` for audit events), and
  2. returned to the agent as a normal tool error starting with
     `[WORKTREE ISOLATION REFUSAL - <check>]` so the model can correct itself.

Only the sync methods are overridden: deepagents' async variants (`aread`, `aexecute`, ...)
delegate to them via `asyncio.to_thread`, so both paths are guarded.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from deepagents.backends import FilesystemBackend, LocalShellBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.middleware.filesystem import FilesystemPermission

from app.domains.guardrails.isolation_policy import PolicyVerdict, WorktreeIsolationPolicy
from app.domains.guardrails.permissions import PermissionRules

logger = logging.getLogger("abyss.audit.deepagent_backend")

REFUSAL_PREFIX = "[WORKTREE ISOLATION REFUSAL"
REDACTED = "[REDACTED]"
REFUSED_EXIT_CODE = 126

_SENSITIVE_ENV_KEY_RE = re.compile(
    r"(?:AUTHORIZATION|API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|"
    r"CREDENTIAL|ACCESS_KEY|SIGNING_KEY|(?:^|_)KEY$|"
    r"^GIT_CONFIG_(?:COUNT|KEY_\d+|VALUE_\d+)$)",
    re.IGNORECASE,
)
_MIN_SECRET_LEN = 6

# deepagents-level permission rules (enforced by `create_deep_agent(permissions=...)` in Phase 3).
# The backend enforces the same intent itself, so it stays safe even without these.
DEFAULT_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/.env*", "/**/.env*", "/.git/**"],
        mode="deny",
    ),
]

# Secret files at any depth (`.env`, `.env.local`, `config/.env.prod`, ...).
_SECRET_FILE_RULES = ("{tool}(.env*)", "{tool}(*/.env*)")


def default_backend_permissions() -> PermissionRules:
    """`PermissionRules.default()` + `.env*` protection at any depth for Read/Edit/Write."""
    rules = PermissionRules.default()
    extra = [rule.format(tool=tool) for tool in ("Read", "Edit", "Write") for rule in _SECRET_FILE_RULES]
    return PermissionRules(deny=[*rules.deny, *extra], ask=list(rules.ask))


def _safe_env(worktree_root: str | Path) -> dict[str, str]:
    """Subprocess env for agent shell commands: secrets scrubbed, worktree/.venv first on PATH."""
    root = Path(worktree_root).resolve()
    env = {k: v for k, v in os.environ.items() if not _SENSITIVE_ENV_KEY_RE.search(k)}
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"

    bin_name = "Scripts" if os.name == "nt" else "bin"
    path_parts: list[str] = []
    worktree_venv_bin = root / ".venv" / bin_name
    if worktree_venv_bin.is_dir():
        path_parts.append(str(worktree_venv_bin))
    interpreter_bin = str(Path(sys.executable).parent)
    if interpreter_bin not in path_parts:
        path_parts.append(interpreter_bin)
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def collect_env_secrets(environ: dict[str, str] | None = None) -> list[str]:
    """Values of sensitive-looking environment variables (API keys, tokens, ...)."""
    source = os.environ if environ is None else environ
    return [
        value
        for key, value in source.items()
        if _SENSITIVE_ENV_KEY_RE.search(key) and value and len(value) >= _MIN_SECRET_LEN
    ]


@dataclass(frozen=True)
class BlockedAttempt:
    """One refused tool call (also what `on_block` receives)."""

    operation: str  # "read" | "write" | "edit" | "delete" | "ls" | "glob" | "grep" | "execute" | ...
    target: str
    check_name: str
    reason: str
    remediation_hint: str = ""
    timestamp: float = field(default_factory=time.time)

    def feedback(self) -> str:
        message = f"{REFUSAL_PREFIX} - {self.check_name}] {self.reason}"
        if self.remediation_hint:
            message += f"\nHint: {self.remediation_hint}"
        return message


class _WorktreeGuardMixin:
    """Path / permission / secret guards shared by the read-write and read-only backends."""

    cwd: Path  # provided by FilesystemBackend

    def _init_guard(
        self,
        worktree_root: str | Path,
        main_checkout_root: str | Path | None,
        *,
        permissions: PermissionRules | None,
        secrets: Iterable[str],
        on_block: Callable[[BlockedAttempt], None] | None,
    ) -> None:
        self.worktree_root = Path(worktree_root).resolve()
        main_root = Path(main_checkout_root).resolve() if main_checkout_root else self.worktree_root.parent
        self.policy = WorktreeIsolationPolicy(self.worktree_root, main_root)
        self.permissions = permissions or default_backend_permissions()
        self.secrets = sorted(
            {s for s in [*secrets, *collect_env_secrets()] if s and len(s) >= _MIN_SECRET_LEN},
            key=len,
            reverse=True,  # replace longer secrets first so substrings don't leave fragments
        )
        self.on_block = on_block
        self.blocked_attempts: list[BlockedAttempt] = []

    # ------------------------------------------------------------------ helpers
    def _real_path(self, virtual_path: str | None) -> Path:
        key = (virtual_path or "/").replace("\\", "/")
        return self.worktree_root / key.lstrip("/")

    def _block(self, operation: str, target: str, check_name: str, reason: str, hint: str = "") -> str:
        attempt = BlockedAttempt(operation, target, check_name, reason, hint)
        self.blocked_attempts.append(attempt)
        logger.warning("blocked %s %r by %s: %s", operation, target, check_name, reason)
        if self.on_block is not None:
            try:
                self.on_block(attempt)
            except Exception:  # noqa: BLE001 - auditing must never break the agent loop
                logger.exception("on_block callback failed")
        return attempt.feedback()

    def _block_verdict(self, operation: str, target: str, verdict: PolicyVerdict) -> str:
        return self._block(operation, target, verdict.check_name, verdict.reason, verdict.remediation_hint)

    def _permission_refusal(self, operation: str, tool: str, target: str, subject: str) -> str | None:
        decision = self.permissions.evaluate(tool, subject)
        if decision.decision == "allow":
            return None
        hint = (
            "This action requires human approval and is not available to autonomous agents."
            if decision.decision == "ask"
            else "This path/command is denied by the project permission rules."
        )
        return self._block(operation, target, f"permission:{decision.decision}", decision.reason, hint)

    def _relative_subject(self, real: Path) -> str:
        resolved = real.resolve()
        if resolved.is_relative_to(self.worktree_root):
            return resolved.relative_to(self.worktree_root).as_posix() or "."
        return str(resolved)

    def _guard_path(self, operation: str, virtual_path: str | None) -> str | None:
        """Return refusal feedback if the path operation must be blocked, else None."""
        target = virtual_path or "/"
        real = self._real_path(virtual_path)
        writes = operation in ("write", "edit", "delete", "upload")
        verdict = self.policy.validate_file_edit(real) if writes else self.policy.validate_file_read(real)
        if not verdict.allowed:
            return self._block_verdict(operation, target, verdict)
        tool = {"write": "Write", "upload": "Write", "delete": "Write", "edit": "Edit"}.get(operation, "Read")
        return self._permission_refusal(operation, tool, target, self._relative_subject(real))

    def _is_visible(self, virtual_path: str) -> bool:
        """Silent filter for listing/search results (.git internals, secret files, symlink escapes)."""
        real = self._real_path(virtual_path)
        if not self.policy.validate_file_read(real).allowed:
            return False
        return self.permissions.evaluate("Read", self._relative_subject(real)).decision == "allow"

    def _scrub(self, text: str) -> str:
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    # ------------------------------------------------------------------ reads
    def ls(self, path: str) -> LsResult:
        if refusal := self._guard_path("ls", path):
            return LsResult(error=refusal, entries=None)
        result = super().ls(path)  # type: ignore[misc]
        if result.entries is not None:
            result.entries = [e for e in result.entries if self._is_visible(e["path"])]
        return result

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if refusal := self._guard_path("read", file_path):
            return ReadResult(error=refusal)
        result = super().read(file_path, offset, limit)  # type: ignore[misc]
        data = result.file_data
        if data is not None and data.get("encoding") == "utf-8":
            data["content"] = self._scrub(data["content"])
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        if refusal := self._guard_path("glob", path):
            return GlobResult(error=refusal, matches=None)
        result = super().glob(pattern, path)  # type: ignore[misc]
        if result.matches is not None:
            result.matches = [m for m in result.matches if self._is_visible(m["path"])]
        return result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        if refusal := self._guard_path("grep", path):
            return GrepResult(error=refusal, matches=None)
        result = super().grep(  # type: ignore[misc]
            pattern, path, glob, max_count=max_count, context_lines=context_lines
        )
        if result.matches is not None:
            visible = []
            for match in result.matches:
                if not self._is_visible(match["path"]):
                    continue
                match["text"] = self._scrub(match["text"])
                for side in ("context_before", "context_after"):
                    for ctx in match.get(side, []) or []:
                        ctx["text"] = self._scrub(ctx["text"])
                visible.append(match)
            result.matches = visible
        return result

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        allowed: list[str] = []
        refused: dict[str, FileDownloadResponse] = {}
        for p in paths:
            if self._guard_path("download", p):
                refused[p] = FileDownloadResponse(path=p, content=None, error="permission_denied")
            else:
                allowed.append(p)
        fetched = {r.path: r for r in super().download_files(allowed)} if allowed else {}  # type: ignore[misc]
        return [refused.get(p) or fetched.get(p) or FileDownloadResponse(path=p, error="file_not_found") for p in paths]


class GuardedWorktreeBackend(_WorktreeGuardMixin, LocalShellBackend):
    """deepagents 기본 도구 전체에 4-Check 가드레일 + 시크릿 스크러빙 (builder / worker)."""

    def __init__(
        self,
        worktree_root: str | Path,
        main_checkout_root: str | Path | None = None,
        *,
        permissions: PermissionRules | None = None,
        secrets: Iterable[str] = (),
        on_block: Callable[[BlockedAttempt], None] | None = None,
        timeout: int = 120,
        max_output_chars: int = 12_000,
    ) -> None:
        super().__init__(
            root_dir=worktree_root,
            virtual_mode=True,
            timeout=timeout,
            env=_safe_env(worktree_root),
            inherit_env=False,
        )
        self._init_guard(
            worktree_root,
            main_checkout_root,
            permissions=permissions,
            secrets=secrets,
            on_block=on_block,
        )
        self.max_output_chars = max_output_chars

    # ------------------------------------------------------------------ writes
    def write(self, file_path: str, content: str) -> WriteResult:
        if refusal := self._guard_path("write", file_path):
            return WriteResult(error=refusal)
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if refusal := self._guard_path("edit", file_path):
            return EditResult(error=refusal)
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        if refusal := self._guard_path("delete", file_path):
            return DeleteResult(error=refusal)
        return super().delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        allowed: list[tuple[str, bytes]] = []
        refused: dict[str, FileUploadResponse] = {}
        for path, data in files:
            if self._guard_path("upload", path):
                refused[path] = FileUploadResponse(path=path, error="permission_denied")
            else:
                allowed.append((path, data))
        written = {r.path: r for r in super().upload_files(allowed)} if allowed else {}
        return [refused.get(p) or written[p] for p, _ in files]

    # ------------------------------------------------------------------ shell
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if refusal := self._permission_refusal("execute", "Bash", command, command):
            return ExecuteResponse(output=refusal, exit_code=REFUSED_EXIT_CODE)
        verdict = self.policy.validate_shell_command(command, cwd=self.worktree_root)
        if not verdict.allowed:
            return ExecuteResponse(
                output=self._block_verdict("execute", command, verdict), exit_code=REFUSED_EXIT_CODE
            )

        response = super().execute(command, timeout=timeout)
        output = self._scrub(response.output or "")
        truncated = response.truncated
        if len(output) > self.max_output_chars:
            half = self.max_output_chars // 2
            omitted = len(output) - 2 * half
            output = f"{output[:half]}\n... [{omitted} chars truncated] ...\n{output[-half:]}"
            truncated = True
        return ExecuteResponse(output=output, exit_code=response.exit_code, truncated=truncated)


class ReadOnlyGuardedBackend(_WorktreeGuardMixin, FilesystemBackend):
    """Reviewer backend: execute 없음(SandboxBackendProtocol 미구현) + 모든 쓰기 거부."""

    def __init__(
        self,
        worktree_root: str | Path,
        main_checkout_root: str | Path | None = None,
        *,
        permissions: PermissionRules | None = None,
        secrets: Iterable[str] = (),
        on_block: Callable[[BlockedAttempt], None] | None = None,
    ) -> None:
        super().__init__(root_dir=worktree_root, virtual_mode=True)
        self._init_guard(
            worktree_root,
            main_checkout_root,
            permissions=permissions,
            secrets=secrets,
            on_block=on_block,
        )

    def _read_only(self, operation: str, target: str) -> str:
        return self._block(
            operation,
            target,
            "read_only",
            f"Reviewer backend is read-only; '{operation}' is not allowed.",
            "Report the problem as a review finding instead of changing files.",
        )

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=self._read_only("write", file_path))

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=self._read_only("edit", file_path))

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=self._read_only("delete", file_path))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        for path, _ in files:
            self._read_only("upload", path)
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]
