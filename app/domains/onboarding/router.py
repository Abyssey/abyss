"""
app/domains/onboarding/router.py
FastAPI Router for Repository Architecture Analysis (Archify IR) and Socratic Onboarding Q&A.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.container import get_onboarding_service
from app.domains.onboarding.schemas import RepoOnboardRequest, SocraticAnswerRequest
from app.domains.onboarding.service import OnboardingService

router = APIRouter(prefix="/api/repo-onboard", tags=["Onboarding · Internal Git & Socratic Analysis"])


@router.post("/analyze")
async def analyze_repository_for_onboarding(
    req: RepoOnboardRequest,
    onboarding_service: OnboardingService = Depends(get_onboarding_service),
) -> dict[str, Any]:
    try:
        return await onboarding_service.analyze(req.repo_url_or_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=onboarding_service.sanitize_error(exc),
        ) from exc


@router.post("/answer")
async def evaluate_socratic_answer(
    req: SocraticAnswerRequest,
    onboarding_service: OnboardingService = Depends(get_onboarding_service),
) -> dict[str, Any]:
    try:
        return await onboarding_service.evaluate_answer(
            repo_url_or_path=req.repo_url_or_path,
            question_id=req.question_id,
            selected_index=req.selected_index,
            reflection=req.reflection,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=onboarding_service.sanitize_error(exc),
        ) from exc
