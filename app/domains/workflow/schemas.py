"""
app/domains/workflow/schemas.py
Pydantic Request & Response DTOs for Workflow, Act 2 Visual Build, Live Preview, and Act 4 Review.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class WorkflowTriggerPayload(BaseModel):
    goal: str = "Build RepoOnboard: Local & Internal Git Codebase Onboarding Service"
    engine_mode: str = "agent_runtime"


class BuildTaskRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    repo_url_or_path: str = "demo"
    instruction: str = "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"
    preset: Literal["project_map_app", "inspection_dashboard", "custom"] = Field(
        default="project_map_app",
        validation_alias=AliasChoices("preset", "preset_id"),
    )
    base_branch: str = "main"
    use_code_mode: bool = False
    push_to_remote: bool = True
    plan_review: Literal["request_review", "always_proceed"] | None = None


class PlanApproveRequest(BaseModel):
    comments: str | list[str] | None = None
    proceed: bool = True


class BuildTestResult(BaseModel):
    exit_code: int
    passed: bool
    output: str


class BuildTaskResponse(BaseModel):
    task_id: str
    preset: str
    status: Literal["completed", "awaiting_plan_review", "failed"]
    repo_name: str
    source_type: str
    branch_name: str
    base_branch: str
    worktree_path: str
    commit_hash: str
    changed_files: list[str]
    git_diff: str
    test_result: BuildTestResult
    guardrail_checks: list[dict[str, Any]]
    pushed_to_remote: bool
    remote_url: str
    preview_url: str | None = None
    preview_file: str | None = None
    provider_used: str
    summary: str
    mr_description: str = ""
    artifacts: dict[str, str] = Field(default_factory=dict)
    todos: list[dict[str, str]] = Field(default_factory=list)
    turn_trace: list[dict[str, Any]] = Field(default_factory=list)
    subagent_calls: list[dict[str, Any]] = Field(default_factory=list)
    pytest_gate_runs: list[dict[str, Any]] = Field(default_factory=list)
    rubric_evaluations: list[dict[str, Any]] = Field(default_factory=list)


class ReviewTaskRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    repo_url_or_path: str = "demo"
    branch_name: str | None = None
    base_branch: str = "main"
    commit_range: str | None = None
    raw_diff: str | None = Field(
        default=None,
        validation_alias=AliasChoices("raw_diff", "diff"),
    )
    preset: Literal["defective_mr", "clean_branch", "defective_dashboard_mr", "latest_build_branch"] | None = Field(
        default=None,
        validation_alias=AliasChoices("preset", "preset_id"),
    )
    run_tests: bool = True


class ReviewFindingDTO(BaseModel):
    severity: Literal["high", "medium", "low"]
    file: str
    line: int = 1
    comment: str
    suggestion: str = ""


class ReviewFindings(BaseModel):
    """Structured output schema produced by the read-only `deepagents` Reviewer."""

    summary: str = Field(default="", description="Concise Korean summary of the code review.")
    findings: list[ReviewFindingDTO] = Field(
        default_factory=list,
        description="Concrete file:line defects, security leaks, or missing test findings.",
    )


class DiffStatsDTO(BaseModel):
    files_changed: int
    additions: int
    deletions: int
    files: list[str] = Field(default_factory=list)


class ReviewTaskResponse(BaseModel):
    review_id: str
    source_type: str
    target: str
    decision: Literal["APPROVE", "REQUEST_CHANGES"]
    score: int
    summary: str
    findings: list[ReviewFindingDTO]
    diff_stats: DiffStatsDTO
    test_result: dict[str, Any] | None = None
    rubric_breakdown: list[dict[str, Any]]
    provider_used: str
    read_only_verified: bool = True
