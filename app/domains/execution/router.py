"""
app/domains/execution/router.py
FastAPI Router for the Code Mode (`REPL`) & Guardrails Interactive Lab.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core.container import get_code_mode_service
from app.domains.execution.schemas import CodeModeRunRequest
from app.domains.execution.service import CodeModeLabService

router = APIRouter(prefix="/api/code-mode", tags=["Execution · Code Mode (REPL) & Guardrails Lab"])


@router.get("/scenarios")
async def get_code_mode_scenarios(
    code_mode_service: CodeModeLabService = Depends(get_code_mode_service),
) -> dict[str, Any]:
    return code_mode_service.get_scenarios_payload()


@router.post("/run")
async def run_code_mode_snippet(
    req: CodeModeRunRequest,
    code_mode_service: CodeModeLabService = Depends(get_code_mode_service),
) -> dict[str, Any]:
    return await code_mode_service.run_snippet(
        code=req.code,
        scenario_id=req.scenario_id,
        reset_sandbox=req.reset_sandbox,
        simulate_slow_hook=req.simulate_slow_hook,
        hook_on_failure=req.hook_on_failure,
    )
