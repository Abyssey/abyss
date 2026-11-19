"""
app/domains/onboarding/schemas.py
Pydantic Request DTOs for the Onboarding Bounded Context.
"""

from __future__ import annotations

from pydantic import BaseModel


class RepoOnboardRequest(BaseModel):
    repo_url_or_path: str = "self"


class SocraticAnswerRequest(BaseModel):
    repo_url_or_path: str = ""
    question_id: str
    selected_index: int
    reflection: str = ""
