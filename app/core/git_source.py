"""
app/core/git_source.py
Zero-Leak Internal Git & Self-Hosted GitLab Source Resolver (Shared Kernel).

Supports two primary real-world usage modes plus an offline self-contained test fixture:
  1. Local Dev Folder (Primary #1): `self`, `demo`, or ANY user-supplied local folder
     path — including plain non-git folders for read-only analysis and isolated worktrees
     for git repositories.
  2. Internal Self-Hosted GitLab (Primary #2): `https://gitlab.internal/...`, `http://...`,
     or `ssh://`/`git@` URLs with Git 2.31+ `GIT_CONFIG_COUNT` host-scoped auth headers,
     corporate TLS CA bundle / SSL verify toggle, and `GIT_TERMINAL_PROMPT=0`.
  3. Offline Demo Fixture: `internal-origin` / `file:///.../internal_origin.git`.

Security invariants:
  - Tokens are NEVER embedded in URLs, CLI `-c` argv, or `.git/config`.
  - Auth headers are host-scoped via `http.<origin>/.extraHeader` using env vars only.
  - All `git` invocations pass `--` before target URLs/paths and reject leading `-` or CRLF.
  - `sanitize_git_text` redacts raw and base64-encoded secrets from all outputs/exceptions.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from app.core.config import AppSettings, load_settings

SourceType = Literal[
    "local_path",
    "internal_git_bare",
    "internal_gitlab",
    "internal_git_ssh",
    "remote_git_http",
]

_URL_USERINFO_RE = re.compile(r"(https?://)[^/\s@]+@")
_AUTH_HEADER_RE = re.compile(r"(?i)(Authorization:\s*(?:Basic|Bearer)\s+)\S+")
_PRIVATE_TOKEN_RE = re.compile(r"(?i)(PRIVATE-TOKEN:\s*)\S+")
_GLPAT_RE = re.compile(r"\bfake-token-[A-Za-z0-9_-]{6,}\b")
_SHORT_GITLAB_PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
_PUBLIC_GIT_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})


class InternalGitSourceError(ValueError):
    """Raised when a Git source target cannot be validated, resolved, or cloned safely."""


def sanitize_git_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact raw/base64 tokens, URL credentials, and auth headers from git outputs."""
    if not text:
        return ""
    sanitized = str(text)
    for secret in secrets:
        if not secret:
            continue
        sanitized = sanitized.replace(secret, "***")
        encoded_oauth = base64.b64encode(f"oauth2:{secret}".encode("utf-8")).decode("ascii")
        sanitized = sanitized.replace(encoded_oauth, "***")
        encoded_colon = base64.b64encode(f":{secret}".encode("utf-8")).decode("ascii")
        sanitized = sanitized.replace(encoded_colon, "***")
        url_quoted = quote(secret, safe="")
        if url_quoted != secret:
            sanitized = sanitized.replace(url_quoted, "***")
    sanitized = _URL_USERINFO_RE.sub(r"\1***@", sanitized)
    sanitized = _AUTH_HEADER_RE.sub(r"\1***", sanitized)
    sanitized = _PRIVATE_TOKEN_RE.sub(r"\1***", sanitized)
    sanitized = _GLPAT_RE.sub("fake-token-***", sanitized)
    return sanitized


def _validate_safe_arg(value: str, label: str = "target") -> str:
    cleaned = value.strip()
    if any(ch in cleaned for ch in ("\r", "\n", "\x00")):
        raise InternalGitSourceError(
            f"안전하지 않은 제어 문자(CRLF/NULL)가 {label}에 포함되어 거부되었습니다."
        )
    if cleaned.startswith("-"):
        raise InternalGitSourceError(
            f"'-'로 시작하는 {label}은 Git CLI 옵션 주입 방지를 위해 허용되지 않습니다."
        )
    return cleaned


def _strip_url_userinfo(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _is_private_or_internal_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    low = hostname.lower()
    if low in ("localhost", "host.docker.internal") or low.endswith(
        (".local", ".internal", ".corp", ".lan", ".intranet")
    ):
        return True
    if "gitlab" in low and low != "gitlab.com":
        return True
    try:
        ip = ipaddress.ip_address(low)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def _repo_name_from_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or url
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if ":" in leaf and url.startswith("git@"):
        leaf = leaf.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return leaf.removesuffix(".git") or "repository"


@dataclass
class ResolvedGitSource:
    """Resolved local folder or cloned internal Git repository ready for inspection/worktrees."""

    repo_name: str
    local_path: Path
    source_type: SourceType
    display_source: str
    remote_url: str | None = None
    is_git_repo: bool = True
    is_temporary: bool = False
    cleanup_dir: Path | None = None

    def cleanup(self) -> None:
        if self.is_temporary and self.cleanup_dir and self.cleanup_dir.exists():
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)

    def __enter__(self) -> "ResolvedGitSource":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.cleanup()


class InternalGitSourceResolver:
    """
    Resolves local development folders (git or plain non-git), local bare remotes,
    and internal self-hosted GitLab URLs with zero-leak credential/TLS handling.
    """

    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or load_settings()

    def _secrets(self) -> tuple[str, ...]:
        token = self.settings.internal_gitlab_token.strip()
        return (token,) if token else ()

    def _should_attach_auth(self, remote_url: str) -> bool:
        token = self.settings.internal_gitlab_token.strip()
        if not token:
            return False
        parsed = urlsplit(remote_url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return False

        if self.settings.internal_gitlab_url:
            cfg_parsed = urlsplit(self.settings.internal_gitlab_url)
            cfg_port = cfg_parsed.port or (443 if cfg_parsed.scheme.lower() == "https" else 80)
            target_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            return (
                parsed.scheme.lower() == cfg_parsed.scheme.lower()
                and (parsed.hostname or "").lower() == (cfg_parsed.hostname or "").lower()
                and target_port == cfg_port
            )

        return parsed.hostname.lower() not in _PUBLIC_GIT_HOSTS

    def build_git_env(self, remote_url: str | None = None) -> dict[str, str]:
        """
        Build a non-interactive Git subprocess environment using Git 2.31+
        `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` env variables.
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_ASKPASS"] = ""

        configs: list[tuple[str, str]] = []

        if remote_url and self._should_attach_auth(remote_url):
            parsed = urlsplit(remote_url)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = f"{host}:{parsed.port}" if parsed.port else host
            origin = f"{parsed.scheme.lower()}://{netloc}/"
            token = self.settings.internal_gitlab_token.strip()
            if self.settings.internal_git_auth_mode == "private_token":
                header_value = f"PRIVATE-TOKEN: {token}"
            else:
                b64 = base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")
                header_value = f"Authorization: Basic {b64}"
            configs.append((f"http.{origin}.extraHeader", header_value))

        if self.settings.internal_git_ca_bundle:
            ca_bundle = self.settings.internal_git_ca_bundle
            configs.append(("http.sslCAInfo", ca_bundle))
            env["GIT_SSL_CAINFO"] = ca_bundle

        if not self.settings.internal_git_ssl_verify:
            configs.append(("http.sslVerify", "false"))
            env["GIT_SSL_NO_VERIFY"] = "1"

        if remote_url and (remote_url.startswith("ssh://") or remote_url.startswith("git@")):
            env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")

        # Clear stale GIT_CONFIG_* keys so only our clean config list applies
        for k in list(env.keys()):
            if k == "GIT_CONFIG_COUNT" or k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
                env.pop(k, None)

        if configs:
            env["GIT_CONFIG_COUNT"] = str(len(configs))
            for idx, (cfg_key, cfg_val) in enumerate(configs):
                env[f"GIT_CONFIG_KEY_{idx}"] = cfg_key
                env[f"GIT_CONFIG_VALUE_{idx}"] = cfg_val

        return env

    def resolve_target_spec(self, target: str) -> tuple[str, SourceType, str]:
        """Classify and normalize a target alias, local path, bare remote, or GitLab URL."""
        raw = (target or "").strip()
        if not raw or raw in (".", "self", "app", "abyss", "niisow/abyss"):
            root = self.settings.repo_root.resolve()
            return str(root), "local_path", f"local:{root.name}"

        cleaned = _validate_safe_arg(raw, label="저장소 경로/URL")

        if cleaned == "demo":
            ws = self.settings.demo_workspace.resolve()
            return str(ws), "local_path", f"local:{ws.name}"

        if cleaned == "internal-origin":
            bare = self.settings.internal_bare_remote.resolve()
            uri = bare.as_uri()
            return uri, "internal_git_bare", uri

        if cleaned.startswith("file://"):
            return cleaned, "internal_git_bare", cleaned

        if cleaned.startswith(("http://", "https://")):
            parsed = urlsplit(cleaned)
            clean_url = _strip_url_userinfo(cleaned)
            source_type: SourceType = "remote_git_http"
            if self.settings.internal_gitlab_url:
                cfg_parsed = urlsplit(self.settings.internal_gitlab_url)
                if (parsed.hostname or "").lower() == (cfg_parsed.hostname or "").lower():
                    source_type = "internal_gitlab"
            if source_type != "internal_gitlab" and _is_private_or_internal_host(parsed.hostname):
                source_type = "internal_gitlab"
            display = sanitize_git_text(clean_url, self._secrets())
            return clean_url, source_type, display

        if cleaned.startswith(("ssh://", "git@")):
            return cleaned, "internal_git_ssh", sanitize_git_text(cleaned, self._secrets())

        # Local filesystem path (supports both git repos and plain non-git folders!)
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            rel_from_repo = (self.settings.repo_root / candidate).resolve()
            if rel_from_repo.exists():
                candidate = rel_from_repo
            else:
                candidate = candidate.resolve()
        else:
            candidate = candidate.resolve()

        if candidate.exists():
            if not candidate.is_dir():
                raise InternalGitSourceError(
                    f"지정한 경로가 디렉토리가 아닙니다: {cleaned}"
                )
            return str(candidate), "local_path", str(candidate)

        # Internal GitLab shorthand like `team/service-repo` when INTERNAL_GITLAB_URL is configured
        if self.settings.internal_gitlab_url and _SHORT_GITLAB_PATH_RE.fullmatch(cleaned):
            base = self.settings.internal_gitlab_url.rstrip("/")
            slug = cleaned.removesuffix(".git")
            url = f"{base}/{slug}.git"
            display = sanitize_git_text(url, self._secrets())
            return url, "internal_gitlab", display

        raise InternalGitSourceError(
            f"로컬 폴더 또는 저장소를 찾을 수 없습니다: {cleaned}. "
            "사용 가능한 입력: 'self'(현재 프로젝트), 'demo'(샘플 저장소), "
            "로컬 폴더 경로(예: /path/to/my-project), 또는 사내 GitLab URL(https://gitlab.internal/...)."
        )

    def _read_local_origin_url(self, local_path: Path) -> str | None:
        if not (local_path / ".git").exists():
            return None
        proc = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(local_path),
            capture_output=True,
            text=True,
            check=False,
        )
        raw = proc.stdout.strip()
        if proc.returncode != 0 or not raw:
            return None
        return sanitize_git_text(_strip_url_userinfo(raw), self._secrets())

    def _format_clone_error(self, display_source: str, stderr: str) -> str:
        clean_err = sanitize_git_text(stderr.strip(), self._secrets())
        low = clean_err.lower()
        if any(tok in low for tok in ("certificate", "ssl", "x509", "self signed", "unable to get local issuer")):
            hint = (
                "사내 TLS 인증서 검증 실패입니다. `.env`에 사내 루트 CA 경로(`INTERNAL_GIT_CA_BUNDLE`)를 "
                "설정하거나 개발 테스트 시 `INTERNAL_GIT_SSL_VERIFY=false`를 지정하세요."
            )
        elif any(tok in low for tok in ("401", "403", "authentication failed", "could not read username", "permission denied")):
            hint = (
                "사내 GitLab 인증 실패입니다. `.env`의 `INTERNAL_GITLAB_TOKEN`(read_repository 권한)과 "
                "`INTERNAL_GITLAB_URL` 설정을 확인하세요."
            )
        else:
            hint = (
                "네트워크 또는 원격 저장소 접속에 실패했습니다. 로컬 개발 폴더(`self`, `demo`, `/path/to/folder`)로 "
                "바로 테스트하거나 사내 GitLab URL(`INTERNAL_GITLAB_URL`)을 확인하세요."
            )
        return f"저장소 클론 실패 ({display_source}): {clean_err or 'git clone returned non-zero'} — {hint}"

    def resolve(
        self,
        target: str,
        *,
        shallow: bool = True,
        branch: str | None = None,
        fetch_all_branches: bool = False,
        scratch_parent: Path | None = None,
    ) -> ResolvedGitSource:
        """Resolve a target into a local directory path (cloning remotes into managed scratch)."""
        resolved_target, source_type, display_source = self.resolve_target_spec(target)
        secrets = self._secrets()

        if source_type == "local_path":
            local_path = Path(resolved_target).resolve()
            is_git = (local_path / ".git").exists()
            return ResolvedGitSource(
                repo_name=local_path.name,
                local_path=local_path,
                source_type="local_path",
                display_source=display_source,
                remote_url=self._read_local_origin_url(local_path),
                is_git_repo=is_git,
                is_temporary=False,
                cleanup_dir=None,
            )

        if branch is not None:
            branch = _validate_safe_arg(branch, label="브랜치 이름")

        clone_parent = (
            Path(scratch_parent).resolve()
            if scratch_parent is not None
            else (self.settings.demo_workspace / ".abyss" / "git_clones").resolve()
        )
        clone_parent.mkdir(parents=True, exist_ok=True)
        dest = Path(tempfile.mkdtemp(prefix="git_src_", dir=str(clone_parent)))

        cmd = ["git", "clone"]
        if shallow and not fetch_all_branches:
            cmd.extend(["--depth", "1"])
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend(["--", resolved_target, str(dest)])

        env = self.build_git_env(resolved_target)
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.settings.internal_git_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise InternalGitSourceError(
                sanitize_git_text(
                    f"저장소 클론 시간 초과 ({display_source}, {self.settings.internal_git_timeout_s}s): {exc}",
                    secrets,
                )
            ) from exc

        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            combined_err = (proc.stderr or "") + "\n" + (proc.stdout or "")
            raise InternalGitSourceError(self._format_clone_error(display_source, combined_err))

        if fetch_all_branches:
            subprocess.run(
                ["git", "fetch", "--all", "--prune"],
                cwd=str(dest),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.settings.internal_git_timeout_s,
                check=False,
            )

        return ResolvedGitSource(
            repo_name=_repo_name_from_url(resolved_target),
            local_path=dest,
            source_type=source_type,
            display_source=sanitize_git_text(display_source, secrets),
            remote_url=sanitize_git_text(resolved_target, secrets),
            is_git_repo=True,
            is_temporary=True,
            cleanup_dir=dest,
        )

    @contextmanager
    def checkout(
        self,
        target: str,
        *,
        shallow: bool = True,
        branch: str | None = None,
        fetch_all_branches: bool = False,
        scratch_parent: Path | None = None,
    ) -> Iterator[ResolvedGitSource]:
        """Context manager that resolves a Git source and cleans up temporary clones."""
        source = self.resolve(
            target,
            shallow=shallow,
            branch=branch,
            fetch_all_branches=fetch_all_branches,
            scratch_parent=scratch_parent,
        )
        try:
            yield source
        finally:
            source.cleanup()


def push_branch_safe(
    repo_path: str | Path,
    branch: str,
    *,
    remote: str = "origin",
    settings: AppSettings | None = None,
    remote_url: str | None = None,
) -> dict[str, Any]:
    """Push a branch to a configured remote using zero-leak env credentials and sanitized outputs."""
    resolved_repo = Path(repo_path).resolve()
    safe_branch = _validate_safe_arg(branch, label="브랜치 이름")
    safe_remote = _validate_safe_arg(remote, label="리모트 이름")
    cfg = settings or load_settings()
    resolver = InternalGitSourceResolver(cfg)
    secrets = resolver._secrets()

    effective_remote_url = remote_url
    if not effective_remote_url:
        url_proc = subprocess.run(
            ["git", "remote", "get-url", "--", safe_remote],
            cwd=str(resolved_repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if url_proc.returncode == 0 and url_proc.stdout.strip():
            effective_remote_url = url_proc.stdout.strip()

    if not effective_remote_url:
        return {
            "pushed": False,
            "branch": safe_branch,
            "remote": safe_remote,
            "remote_url": "",
            "output": "No git remote configured.",
        }

    env = resolver.build_git_env(effective_remote_url)
    proc = subprocess.run(
        ["git", "push", "-u", "--", safe_remote, safe_branch],
        cwd=str(resolved_repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=cfg.internal_git_timeout_s,
        check=False,
    )
    output = sanitize_git_text((proc.stdout or "") + (proc.stderr or ""), secrets).strip()
    clean_url = sanitize_git_text(_strip_url_userinfo(effective_remote_url), secrets)
    return {
        "pushed": proc.returncode == 0,
        "branch": safe_branch,
        "remote": safe_remote,
        "remote_url": clean_url,
        "output": output,
    }
