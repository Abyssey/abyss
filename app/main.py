"""
app/main.py
Composition Root & FastAPI Application Factory (`uvicorn app.main:app --host 0.0.0.0 --port 8000`).

Modern Python DDD Modular Monolith:
- `app/domains/guardrails/`: ★ Focus Domain #1 — 4-Check Worktree Isolation & Permission Splitter (Pure Stdlib)
- `app/domains/execution/` : ★ Focus Domain #2 — Code Mode (`REPL`) Kick & Dual-Mode Agent Runtime
- `app/domains/workflow/`  : Act 2 Visual Build + Live Preview, Act 4 Independent Review, 3-Worktree Teams
- `app/domains/onboarding/`: Act 1 Local Dev Folder (Git/Non-Git) & Internal GitLab AST + Socratic Analysis
- `app/core/`              : Settings, Zero-Leak Internal GitLab Resolver, DI Container, Multi-LLM Rotator
"""

from __future__ import annotations

import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import AppSettings, load_settings
from app.core.container import build_container
from app.core.llm.router import router as llm_router
from app.domains.execution.router import router as execution_router
from app.domains.onboarding.router import router as onboarding_router
from app.domains.workflow.router import router as workflow_router

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: AppSettings | None = None) -> FastAPI:
    effective_settings = settings or load_settings(repo_root=REPO_ROOT, static_dir=STATIC_DIR)
    container = build_container(settings=effective_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        # 1. Ensure demo workspace has initial merged RepoOnboard files on first boot
        if not (container.demo_workspace / "repo_onboard" / "service.py").exists():
            await container.coordinator.execute_full_workflow(
                "Build RepoOnboard: Local & Internal Git Codebase Onboarding Service"
            )
        # 2. Always guarantee offline bare remote fixture (`internal_origin.git`) is ready
        container.coordinator.ensure_internal_bare_remote()
        # 3. Always reset isolated Code Mode REPL sandbox cleanly
        container.code_mode_service.reset_sandbox()
        yield

    application = FastAPI(
        title="Modern Coding Agent Architecture — Guardrails, Code Mode REPL & Internal Git Worktrees",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.container = container
    application.mount("/static", StaticFiles(directory=str(effective_settings.static_dir)), name="static")

    @application.get("/healthz", tags=["Pod Readiness & Health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "modern-coding-agent-architecture"}

    @application.get("/readyz", tags=["Pod Readiness & Health"])
    async def readyz(request: Request) -> JSONResponse:
        app_container = getattr(request.app.state, "container", None)
        if app_container is None:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "checks": {"container": False}},
            )

        app_container.demo_workspace.mkdir(parents=True, exist_ok=True)
        checks = {
            "container": True,
            "git": shutil.which("git") is not None,
            "static_ui": (app_container.static_dir / "index.html").is_file(),
            "workspace_writable": os.access(app_container.demo_workspace, os.W_OK),
            "internal_bare_remote": (app_container.settings.internal_bare_remote / "HEAD").is_file(),
        }
        all_ready = all(checks.values())
        return JSONResponse(
            status_code=200 if all_ready else 503,
            content={
                "status": "ready" if all_ready else "not_ready",
                "checks": checks,
                "internal_gitlab_configured": app_container.settings.internal_gitlab_configured,
            },
        )

    application.include_router(workflow_router)
    application.include_router(execution_router)
    application.include_router(llm_router)
    application.include_router(onboarding_router)
    return application


app = create_app()
