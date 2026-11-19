"""
app/domains/workflow/router.py
FastAPI Router for Dashboard State, 3-Worktree Team Runs, SSE Stream, Architecture Catalog,
Act 2 Visual Build + Live HTML Preview, and Act 4 Read-Only Independent Code Review.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from app.core.container import (
    AppContainer,
    get_catalog_service,
    get_container,
    get_coordinator,
    get_task_service,
)
from app.domains.workflow.catalog_service import ArchitectureCatalogService
from app.domains.workflow.coordinator import MultiAgentTeamCoordinator
from app.domains.workflow.schemas import (
    BuildTaskRequest,
    ReviewTaskRequest,
    WorkflowTriggerPayload,
)
from app.domains.workflow.task_service import CodingTaskService

router = APIRouter(tags=["Workflow · Build, Live Preview, Review & Agent Teams"])


@router.get("/")
async def serve_index(container: AppContainer = Depends(get_container)) -> FileResponse:
    return FileResponse(container.static_dir / "index.html")


@router.get("/api/state")
async def get_dashboard_state(
    coordinator: MultiAgentTeamCoordinator = Depends(get_coordinator),
) -> dict[str, Any]:
    return coordinator.get_dashboard_state()


@router.post("/api/workflow/run")
async def run_multi_agent_workflow(
    payload: WorkflowTriggerPayload,
    coordinator: MultiAgentTeamCoordinator = Depends(get_coordinator),
) -> dict[str, Any]:
    if coordinator.is_running:
        raise HTTPException(status_code=409, detail="Workflow is already running.")
    coordinator.engine_mode = payload.engine_mode
    return await coordinator.execute_full_workflow(user_goal=payload.goal)


@router.get("/api/events/stream")
async def stream_events(
    coordinator: MultiAgentTeamCoordinator = Depends(get_coordinator),
) -> StreamingResponse:
    queue = coordinator.subscribe_events()

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            coordinator.unsubscribe_events(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/api/learning-core")
async def get_architecture_catalog(
    catalog_service: ArchitectureCatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    return catalog_service.get_catalog()


@router.get("/api/workflow/tasks/presets")
async def get_workflow_task_presets(
    task_service: CodingTaskService = Depends(get_task_service),
) -> dict[str, Any]:
    return task_service.get_presets()


@router.post("/api/workflow/tasks/build")
async def build_coding_task(
    req: BuildTaskRequest,
    task_service: CodingTaskService = Depends(get_task_service),
) -> dict[str, Any]:
    try:
        return await task_service.run_build_task(
            repo_url_or_path=req.repo_url_or_path,
            instruction=req.instruction,
            preset=req.preset,
            base_branch=req.base_branch,
            use_code_mode=req.use_code_mode,
            push_to_remote=req.push_to_remote,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=task_service.sanitize_error(exc)) from exc


@router.post("/api/workflow/tasks/review")
async def review_coding_task(
    req: ReviewTaskRequest,
    task_service: CodingTaskService = Depends(get_task_service),
) -> dict[str, Any]:
    try:
        return await task_service.run_review_task(
            repo_url_or_path=req.repo_url_or_path,
            branch_name=req.branch_name,
            base_branch=req.base_branch,
            commit_range=req.commit_range,
            raw_diff=req.raw_diff,
            preset=req.preset,
            run_tests=req.run_tests,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=task_service.sanitize_error(exc)) from exc


@router.get("/api/workflow/tasks/{task_id}/preview")
async def preview_built_artifact(
    task_id: str,
    task_service: CodingTaskService = Depends(get_task_service),
) -> HTMLResponse:
    try:
        html_content, branch_name = task_service.get_preview_html(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return HTMLResponse(
        content=html_content,
        status_code=200,
        headers={
            "Cache-Control": "no-store",
            "X-Worktree-Branch": branch_name,
            "Content-Security-Policy": "sandbox allow-scripts",
            "X-Content-Type-Options": "nosniff",
        },
    )
