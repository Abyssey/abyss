"""
app/core/config.py
Application settings loader for local development, internal Pod deployment,
and self-hosted GitLab connectivity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

GitAuthMode = Literal["basic_oauth2", "private_token"]


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _parse_auth_mode(value: str | None) -> GitAuthMode:
    cleaned = (value or "basic_oauth2").strip().lower()
    if cleaned == "private_token":
        return "private_token"
    return "basic_oauth2"


@dataclass(frozen=True)
class AppSettings:
    """Immutable runtime settings for the Modern DDD Modular Monolith."""

    repo_root: Path
    static_dir: Path
    demo_workspace: Path
    code_mode_sandbox: Path
    internal_bare_remote: Path
    task_runs_dir: Path
    host: str = "0.0.0.0"
    port: int = 8000
    internal_gitlab_url: str = ""
    internal_gitlab_token: str = ""
    internal_git_auth_mode: GitAuthMode = "basic_oauth2"
    internal_git_ssl_verify: bool = True
    internal_git_ca_bundle: str = ""
    internal_git_timeout_s: int = 30

    @property
    def internal_gitlab_configured(self) -> bool:
        return bool(self.internal_gitlab_url.strip())


def load_settings(
    repo_root: Path | None = None,
    static_dir: Path | None = None,
    demo_workspace: Path | None = None,
    internal_bare_remote: Path | None = None,
) -> AppSettings:
    """Load application settings from arguments and environment variables."""
    resolved_repo_root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parent.parent.parent
    )
    resolved_static_dir = (
        Path(static_dir).resolve()
        if static_dir is not None
        else Path(__file__).resolve().parent.parent / "static"
    )

    env_demo_ws = os.getenv("DEMO_WORKSPACE_DIR", "").strip()
    if demo_workspace is not None:
        resolved_demo_ws = Path(demo_workspace).resolve()
    elif env_demo_ws:
        resolved_demo_ws = Path(env_demo_ws).expanduser().resolve()
    else:
        resolved_demo_ws = (resolved_repo_root / "demo_workspace" / "repo_onboard").resolve()

    env_bare_remote = os.getenv("INTERNAL_BARE_REMOTE", "").strip()
    if internal_bare_remote is not None:
        resolved_bare_remote = Path(internal_bare_remote).resolve()
    elif env_bare_remote:
        resolved_bare_remote = Path(env_bare_remote).expanduser().resolve()
    else:
        resolved_bare_remote = (resolved_demo_ws.parent / "internal_origin.git").resolve()

    code_mode_sandbox = resolved_demo_ws / ".abyss" / "code_mode_sandbox"
    task_runs_dir = resolved_demo_ws.parent / "task_runs"

    gitlab_url = os.getenv("INTERNAL_GITLAB_URL", "").strip().rstrip("/")
    gitlab_token = (
        os.getenv("INTERNAL_GITLAB_TOKEN", "").strip()
        or os.getenv("INTERNAL_GIT_TOKEN", "").strip()
    )

    return AppSettings(
        repo_root=resolved_repo_root,
        static_dir=resolved_static_dir,
        demo_workspace=resolved_demo_ws,
        code_mode_sandbox=code_mode_sandbox,
        internal_bare_remote=resolved_bare_remote,
        task_runs_dir=task_runs_dir,
        host=os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_parse_int(os.getenv("PORT"), 8000),
        internal_gitlab_url=gitlab_url,
        internal_gitlab_token=gitlab_token,
        internal_git_auth_mode=_parse_auth_mode(os.getenv("INTERNAL_GIT_AUTH_MODE")),
        internal_git_ssl_verify=_parse_bool(os.getenv("INTERNAL_GIT_SSL_VERIFY"), True),
        internal_git_ca_bundle=os.getenv("INTERNAL_GIT_CA_BUNDLE", "").strip(),
        internal_git_timeout_s=max(5, _parse_int(os.getenv("INTERNAL_GIT_TIMEOUT_S"), 30)),
    )
