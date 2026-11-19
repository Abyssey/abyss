"""
app/domains/workflow/catalog_service.py
Architecture Source Catalog Service exposing the 2 Highlighted Focus Domains
(`guardrails`, `execution`) + Independent Evaluator Gate and 4 Supporting Modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class ArchitectureCatalogService:
    """
    Loads the live source code and Modern DDD architectural rationale for:
    - 5 Core Architecture Modules across Focus Domain #1 (`guardrails`), Focus Domain #2 (`execution`),
      and the Independent Evaluator Gate (`workflow/evaluator.py`)
    - 4 Supporting Workflow & Shared Kernel Modules
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def get_catalog(self) -> dict[str, list[dict[str, Any]]]:
        core_files: list[dict[str, Any]] = [
            {
                "id": "isolation_policy",
                "title": "★ Focus Domain #1 · 4-Check Git Worktree Isolation Policy",
                "file_path": "app/domains/guardrails/isolation_policy.py",
                "claude_pattern": "Modern Coding Agent Guardrails · 4-Check Worktree Enforcement (Pure Stdlib)",
                "why_different": (
                    "가벼운 Git Worktree(`.abyss/worktrees/<id>`)로 브랜치와 작업 트리를 분리하되, 도구 실행 직전에 "
                    "① File Edit Path(`is_relative_to`, 심볼릭 링크·`../` 차단) ② Command CWD ③ Git Redirect(`git -C`, `--git-dir`, `cd ..`) "
                    "④ Dynamic Command Shape(`${!var}`, `eval`, 백틱, `git $(...)`) 4가지 결정론적 정책을 순수 파이썬 표준 라이브러리로 강제합니다."
                ),
                "ownership": "★ Focus Domain #1 · Guardrails (Pure Stdlib)",
            },
            {
                "id": "permissions",
                "title": "★ Focus Domain #1 · Compound Command Permission Splitter (`deny` / `ask`)",
                "file_path": "app/domains/guardrails/permissions.py",
                "claude_pattern": "Guardrails · Compound Shell Command Segment Evaluation (`deny > ask > allow`)",
                "why_different": (
                    "`cd 'app' && FOO=1 git push origin main` 같은 복합 셸 명령은 단순 접두어 규칙을 우회합니다. "
                    "따옴표와 리다이렉션(`2>&1`)을 보존하면서 `&&`, `||`, `;`, `|` 기준으로 세그먼트를 분리하고 환경변수를 정규화해 "
                    "`Bash(git push*)`, `Write(.env)` 등의 금지 규칙을 각 세그먼트마다 정확히 차단합니다."
                ),
                "ownership": "★ Focus Domain #1 · Guardrails (Pure Stdlib)",
            },
            {
                "id": "code_mode",
                "title": "★ Focus Domain #2 · Claude Code Kick: Code Mode (`REPL`) Executor",
                "file_path": "app/domains/execution/code_mode.py",
                "claude_pattern": "Claude Code Kick · Code Mode (`REPL`) 1-Step Batch Execution & In-Code Filtering",
                "why_different": (
                    "도구 1개마다 LLM 왕복을 반복하는 Direct Tool Calling 대신, 모델이 작성한 비동기 코드 한 덩어리로 "
                    "검색·편집·병렬 읽기(`gather`)·테스트를 한 스텝에 수행하고 마지막 표현식만 모델에 반환합니다. "
                    "정적 AST 검토(STEP 0)와 개별 호출당 권한·격리·PreToolUse 훅 검증(STEP 2)을 결합해 사내 Qwen 3.8 Flash에서도 안전하게 구동합니다."
                ),
                "ownership": "★ Focus Domain #2 · Execution (Claude Code Kick)",
            },
            {
                "id": "agent_runtime",
                "title": "★ Focus Domain #2 · Dual-Mode Agent Runtime & Context Strategy",
                "file_path": "app/domains/execution/runtime.py",
                "claude_pattern": "Dual-Mode Tool Loop · Auto-Compaction Default + Optional Context Reset Handoff",
                "why_different": (
                    "Direct Tool Calling과 Code Mode(`REPL`)를 단일 런타임에서 전환하며 `<think>` 추론 블록을 자동 분리합니다. "
                    "Claude Code CLI의 기본 전략인 Auto-Compaction(`context_strategy='compaction'`)과 "
                    "에이전트 간 인계용 Context Reset + `HandoffArtifact`(`context_strategy='reset_handoff'`)를 모두 지원합니다."
                ),
                "ownership": "★ Focus Domain #2 · Execution (Claude Code Kick)",
            },
            {
                "id": "evaluator_rubric",
                "title": "Workflow Domain · Independent 3-Agent Evaluator & Walkthrough Reviewer",
                "file_path": "app/domains/workflow/evaluator.py",
                "claude_pattern": "Open SWE Independent Reviewer + Guided Diff Walkthrough (`file:line` Findings)",
                "why_different": (
                    "코드를 작성한 에이전트(Generator/Builder)와 검증하는 에이전트(Reviewer)를 완전히 분리해 자기 과대평가를 차단합니다. "
                    "3-Worktree 100점 정량 머지 게이트(40+25+20+15)와 Act 4 MR/브랜치 Diff 리뷰(하드코딩 비밀키, `except: pass`, 테스트 누락 탐지)를 "
                    "순수 표준 라이브러리로 결정론적 실행합니다."
                ),
                "ownership": "Workflow Domain · Independent Gate (Pure Stdlib)",
            },
        ]

        for item in core_files:
            full_path = self.repo_root / item["file_path"]
            item["code"] = (
                full_path.read_text(encoding="utf-8") if full_path.exists() else "# File not found"
            )

        infra_files: list[dict[str, Any]] = [
            {
                "file_path": "app/domains/workflow/worktree_adapter.py",
                "role": "Git Worktree 생성·Lock·Merge 및 오프라인 Bare Remote / Safe Push 어댑터 (Workflow Domain)",
                "ownership": "Workflow Domain · Git Adapter",
            },
            {
                "file_path": "app/domains/workflow/task_service.py",
                "role": "Act 2 시각 앱 빌드(`project_map_app`/`inspection_dashboard`), Live Preview & Act 4 읽기 전용 리뷰 서비스 (Workflow Domain)",
                "ownership": "Workflow Domain · Task & Preview Pipeline",
            },
            {
                "file_path": "app/core/git_source.py",
                "role": "로컬 폴더(Git/Non-Git) 및 사내 GitLab Zero-Leak `GIT_CONFIG_COUNT` 인증·TLS 리졸버 (Shared Kernel)",
                "ownership": "Shared Kernel · Internal Git Security",
            },
            {
                "file_path": "app/core/llm/provider_rotator.py",
                "role": "사내 Qwen 3.8 Flash / Gemini / OpenAI / Mock 멀티 프로바이더 로테이션 라우터 (Shared Kernel)",
                "ownership": "Shared Kernel · Multi-LLM Router",
            },
        ]

        return {
            "core_modules": core_files,
            "infra_modules": infra_files,
        }


# Backward-compatible class alias
LearningCatalogService = ArchitectureCatalogService
