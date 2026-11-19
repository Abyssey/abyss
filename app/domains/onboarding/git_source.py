"""
app/domains/onboarding/git_source.py
Onboarding Domain facade over Shared Kernel `app.core.git_source`.

Supports local dev folders (git or plain non-git), `self`, `demo`, `internal-origin` (`file://`),
and internal self-hosted GitLab URLs with zero-leak auth headers and TLS configuration.
"""

from __future__ import annotations

from app.core.git_source import (
    InternalGitSourceError,
    InternalGitSourceResolver,
    ResolvedGitSource,
    SourceType,
    sanitize_git_text,
)

__all__ = [
    "InternalGitSourceError",
    "InternalGitSourceResolver",
    "ResolvedGitSource",
    "SourceType",
    "sanitize_git_text",
]
