"""
app/domains/onboarding/service.py
Onboarding Domain Service — Local Folder (Git / Non-Git) & Internal GitLab AST Analysis + Socratic Tutor.

Uses in-process `RepoAnalyzer` and `SocraticOnboardingTutor` backed by `InternalGitSourceResolver`,
eliminating stale `demo_workspace` `sys.modules` imports and guaranteeing temporary clone cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import AppSettings, load_settings
from app.domains.onboarding.git_source import (
    InternalGitSourceResolver,
    sanitize_git_text,
)
from app.domains.onboarding.templates import RepoAnalyzer, SocraticOnboardingTutor


class OnboardingService:
    """Analyzes local folders or internal Git repositories and runs 3-stage Socratic onboarding Q&A."""

    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or load_settings()
        self.git_resolver = InternalGitSourceResolver(self.settings)
        self.analyzer = RepoAnalyzer()
        self.tutor = SocraticOnboardingTutor()
        self._cached_package: dict[str, Any] | None = None

    def sanitize_error(self, exc: Exception) -> str:
        secrets = (self.settings.internal_gitlab_token,) if self.settings.internal_gitlab_token else ()
        return sanitize_git_text(str(exc), secrets)

    def _analyze_sync(self, repo_url_or_path: str) -> dict[str, Any]:
        secrets = (self.settings.internal_gitlab_token,) if self.settings.internal_gitlab_token else ()
        with self.git_resolver.checkout(repo_url_or_path, shallow=True) as resolved:
            analysis = self.analyzer.analyze_directory(
                resolved.local_path,
                repo_name=resolved.repo_name,
                source_type=resolved.source_type,
                display_source=sanitize_git_text(resolved.display_source, secrets),
                is_git_repo=resolved.is_git_repo,
            )
            tutor_pkg = self.tutor.build_onboarding_package(analysis)
            combined = {
                "repo_name": analysis["repo_name"],
                "root_path": sanitize_git_text(analysis["display_source"], secrets),
                "analysis": analysis,
                "onboarding_brief": tutor_pkg["onboarding_brief"],
                "socratic_ladder": tutor_pkg["socratic_ladder"],
            }
            self._cached_package = combined
            return combined

    async def analyze(self, repo_url_or_path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._analyze_sync, repo_url_or_path)

    async def evaluate_answer(
        self,
        repo_url_or_path: str,
        question_id: str,
        selected_index: int,
        reflection: str = "",
    ) -> dict[str, Any]:
        if self._cached_package is None:
            await self.analyze(repo_url_or_path or "self")
        assert self._cached_package is not None
        ladder = self._cached_package.get("socratic_ladder", [])
        if not ladder:
            raise ValueError("No Socratic questions available for the analyzed repository.")
        question = next((q for q in ladder if q["id"] == question_id), ladder[0])
        return self.tutor.evaluate_answer(
            question=question,
            selected_index=selected_index,
            user_reflection=reflection,
        )
