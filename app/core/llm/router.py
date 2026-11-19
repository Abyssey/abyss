"""
app/core/llm/router.py
FastAPI Router for Multi-Provider LLM Rotation Configuration and Connectivity Testing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.container import get_rotator
from app.core.llm.provider_rotator import LLMProviderRotator

router = APIRouter(prefix="/api/providers", tags=["LLM Providers & Rotation"])


class ProviderUpdatePayload(BaseModel):
    strategy: str | None = None
    active_single_provider: str | None = None
    role_mapping: dict[str, str] | None = None
    providers: dict[str, dict[str, Any]] | None = None


class ProviderUpdateResponse(BaseModel):
    ok: bool = True
    rotator: dict[str, Any]


@router.post("/config", response_model=ProviderUpdateResponse)
async def update_providers_config(
    payload: ProviderUpdatePayload,
    rotator: LLMProviderRotator = Depends(get_rotator),
) -> dict[str, Any]:
    updated = rotator.update_config(payload.model_dump(exclude_none=True))
    return {"ok": True, "rotator": updated}


@router.post("/test/{provider_id}")
async def test_provider(
    provider_id: str,
    rotator: LLMProviderRotator = Depends(get_rotator),
) -> dict[str, Any]:
    return await asyncio.to_thread(rotator.test_provider_connection, provider_id)
