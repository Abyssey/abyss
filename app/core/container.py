"""
app/core/container.py
Composition Root Dependency Injection Container & FastAPI `Depends()` Providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, Request

from app.core.config import AppSettings, load_settings
from app.core.llm.provider_rotator import LLMProviderRotator
from app.domains.execution.service import CodeModeLabService
from app.domains.onboarding.service import OnboardingService
from app.domains.workflow.catalog_service import ArchitectureCatalogService
from app.domains.workflow.coordinator import MultiAgentTeamCoordinator
from app.domains.workflow.task_service import CodingTaskService


@dataclass
class AppContainer:
    settings: AppSettings
    repo_root: Path
    static_dir: Path
    demo_workspace: Path
    code_mode_sandbox: Path
    rotator: LLMProviderRotator
    coordinator: MultiAgentTeamCoordinator
    task_service: CodingTaskService
    code_mode_service: CodeModeLabService
    catalog_service: ArchitectureCatalogService
    onboarding_service: OnboardingService


def build_container(
    settings: AppSettings | None = None,
    *,
    repo_root: Path | None = None,
    static_dir: Path | None = None,
) -> AppContainer:
    effective_settings = settings or load_settings(repo_root=repo_root, static_dir=static_dir)
    effective_settings.demo_workspace.mkdir(parents=True, exist_ok=True)
    effective_settings.task_runs_dir.mkdir(parents=True, exist_ok=True)

    rotator = LLMProviderRotator()
    coordinator = MultiAgentTeamCoordinator(
        workspace_root=effective_settings.demo_workspace,
        rotator=rotator,
        bare_remote_path=effective_settings.internal_bare_remote,
        settings=effective_settings,
    )
    task_service = CodingTaskService(
        settings=effective_settings,
        rotator=rotator,
        event_callback=coordinator.emit_event,
    )
    code_mode_service = CodeModeLabService(
        sandbox_root=effective_settings.code_mode_sandbox,
        main_checkout_root=effective_settings.demo_workspace,
        event_callback=coordinator.emit_event,
    )
    catalog_service = ArchitectureCatalogService(repo_root=effective_settings.repo_root)
    onboarding_service = OnboardingService(settings=effective_settings)

    return AppContainer(
        settings=effective_settings,
        repo_root=effective_settings.repo_root,
        static_dir=effective_settings.static_dir,
        demo_workspace=effective_settings.demo_workspace,
        code_mode_sandbox=effective_settings.code_mode_sandbox,
        rotator=rotator,
        coordinator=coordinator,
        task_service=task_service,
        code_mode_service=code_mode_service,
        catalog_service=catalog_service,
        onboarding_service=onboarding_service,
    )


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_settings(container: AppContainer = Depends(get_container)) -> AppSettings:
    return container.settings


def get_coordinator(container: AppContainer = Depends(get_container)) -> MultiAgentTeamCoordinator:
    return container.coordinator


def get_rotator(container: AppContainer = Depends(get_container)) -> LLMProviderRotator:
    return container.rotator


def get_task_service(container: AppContainer = Depends(get_container)) -> CodingTaskService:
    return container.task_service


def get_code_mode_service(container: AppContainer = Depends(get_container)) -> CodeModeLabService:
    return container.code_mode_service


def get_catalog_service(
    container: AppContainer = Depends(get_container),
) -> ArchitectureCatalogService:
    return container.catalog_service


def get_onboarding_service(
    container: AppContainer = Depends(get_container),
) -> OnboardingService:
    return container.onboarding_service
