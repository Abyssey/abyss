"""
================================================================================
[MY LEARNING CORE #3] abyss/core/evaluator_rubric.py
Anthropic Three-Agent Architecture — Independent Evaluator Rubric
================================================================================

[구현 메커니즘 포인트]
Anthropic Labs의 "Three-Agent Harness Supports Long-Running Full-Stack AI Development"
아키텍처가 강조하는 핵심 설계는 생성자(Generator/Worker)와 평가자(Evaluator/Reviewer)의
완전한 분리입니다:
  "Separating the agent doing the work from the agent judging it proves to be
   a strong lever to address self-evaluation bias."

작업 에이전트(Worker)에게 자체 검증만 맡기면 자기 결과물을 과대평가(Self-evaluation bias)하거나
실패한 케이스를 우회해 완료 선언을 하는 문제가 발생합니다.
따라서 Abyss는 독립된 **Evaluator(Reviewer) 루브릭**을 통해:
  1. Worktree 브랜치의 실제 `git diff`가 비어 있지 않고 타겟 모듈을 구현했는지
  2. 격리된 Worktree 위에서 실행한 `pytest`가 100% 통과(exit_code == 0)했는지
  3. 동료 에이전트와 메일박스로 합의한 인터페이스/스키마 계약을 준수했는지
  4. 금지된 슬롭/미완성 코드(TODO 방치, 빈 pass 함수 등)가 없는지
를 정량 채점(100점 만점, 80점 이상 & 테스트 통과 시에만 APPROVED)하여 머지 게이트로 작동합니다.
================================================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CriterionScore:
    """개별 평가 기준 채점 결과."""

    name: str
    score: int
    max_score: int
    passed: bool
    comment: str


@dataclass
class ReviewDecision:
    """
    Reviewer(Evaluator) 에이전트가 Worker의 Worktree 브랜치에 대해 내리는 최종 판정.
    `approved=True`여야만 Lead 에이전트가 `main` 브랜치로 `git merge`를 수행합니다.
    """

    branch_name: str
    worker_id: str
    approved: bool
    total_score: int
    criteria: list[CriterionScore] = field(default_factory=list)
    feedback_for_worker: str = ""
    reviewer_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_name": self.branch_name,
            "worker_id": self.worker_id,
            "approved": self.approved,
            "total_score": self.total_score,
            "criteria": [
                {
                    "name": c.name,
                    "score": c.score,
                    "max_score": c.max_score,
                    "passed": c.passed,
                    "comment": c.comment,
                }
                for c in self.criteria
            ],
            "feedback_for_worker": self.feedback_for_worker,
            "reviewer_notes": self.reviewer_notes,
        }


class EvaluatorRubric:
    """
    Anthropic 3-Agent Harness 스타일의 독립 코드 리뷰 & 검증 루브릭.
    결정론적 정적/동적 검증(Diff, Pytest, Contract, Anti-Slop)과 LLM 리뷰어의 코멘트를 결합합니다.
    """

    _STUB_PATTERN = re.compile(r"^\s*\+\s*(?:pass|raise NotImplementedError)\s*$", re.MULTILINE)

    def evaluate_worktree_submission(
        self,
        worker_id: str,
        branch_name: str,
        git_diff: str,
        pytest_exit_code: int,
        pytest_output: str,
        required_symbols: list[str] | None = None,
        mailbox_messages_sent: int = 0,
        llm_critique: str = "",
    ) -> ReviewDecision:
        """
        Worker가 제출한 Worktree 브랜치를 4가지 객관적 기준으로 채점하고 승인/반려를 결정합니다.
        """
        criteria: list[CriterionScore] = []
        required_symbols = required_symbols or []

        # ---------------------------------------------------------------------
        # 기준 1: Automated Test Verification (40점) — 가장 중요한 하드 게이트
        # ---------------------------------------------------------------------
        tests_passed = pytest_exit_code == 0
        test_score = 40 if tests_passed else 0
        criteria.append(
            CriterionScore(
                name="Automated Pytest Verification",
                score=test_score,
                max_score=40,
                passed=tests_passed,
                comment=(
                    "Worktree 격리 환경에서 모든 pytest 스위트를 통과했습니다."
                    if tests_passed
                    else f"pytest 실패 (exit_code={pytest_exit_code}): {pytest_output[-300:]}"
                ),
            )
        )

        # ---------------------------------------------------------------------
        # 기준 2: Git Diff & Substance Check (25점)
        # ---------------------------------------------------------------------
        added_lines = [
            line
            for line in git_diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        has_meaningful_diff = len(added_lines) >= 10
        diff_score = 25 if has_meaningful_diff else (10 if added_lines else 0)
        criteria.append(
            CriterionScore(
                name="Git Diff Substance",
                score=diff_score,
                max_score=25,
                passed=has_meaningful_diff,
                comment=(
                    f"유효한 코드 추가 확인 (+{len(added_lines)} lines in {branch_name})."
                    if has_meaningful_diff
                    else "코드 변경량이 부족하거나 커밋되지 않았습니다."
                ),
            )
        )

        # ---------------------------------------------------------------------
        # 기준 3: Cross-Agent Schema & Symbol Contract (20점)
        # ---------------------------------------------------------------------
        missing_symbols = [sym for sym in required_symbols if sym not in git_diff]
        contract_ok = len(missing_symbols) == 0
        contract_score = 20 if contract_ok else max(0, 20 - 10 * len(missing_symbols))
        if mailbox_messages_sent > 0 and contract_ok:
            contract_comment = (
                f"필수 심볼({', '.join(required_symbols) or 'all'}) 구현 및 "
                f"동료 워커와 메일박스 스키마 동기화({mailbox_messages_sent}건) 완료."
            )
        elif contract_ok:
            contract_comment = f"필수 인터페이스 심볼({', '.join(required_symbols) or 'all'}) 구현 확인."
        else:
            contract_comment = f"누락된 필수 인터페이스 심볼: {', '.join(missing_symbols)}"

        criteria.append(
            CriterionScore(
                name="Interface & Mailbox Contract",
                score=contract_score,
                max_score=20,
                passed=contract_ok,
                comment=contract_comment,
            )
        )

        # ---------------------------------------------------------------------
        # 기준 4: Craft & No Unimplemented Stubs (15점)
        # ---------------------------------------------------------------------
        has_stubs = bool(self._STUB_PATTERN.search(git_diff))
        craft_ok = not has_stubs
        craft_score = 15 if craft_ok else 5
        criteria.append(
            CriterionScore(
                name="Code Craft & Completeness",
                score=craft_score,
                max_score=15,
                passed=craft_ok,
                comment=(
                    "미구현 스텁(pass / NotImplementedError) 없이 완전한 로직으로 작성되었습니다."
                    if craft_ok
                    else "diff 내에 미구현 스텁(pass 또는 NotImplementedError)이 남아 있습니다."
                ),
            )
        )

        total_score = sum(c.score for c in criteria)
        # 승인 조건: 테스트 필수 통과 + 총점 80점 이상 + 필수 심볼 누락 없음
        approved = tests_passed and contract_ok and total_score >= 80

        failed_reasons = [f"- [{c.name}] {c.comment}" for c in criteria if not c.passed]
        if approved:
            feedback = (
                f"[APPROVED ({total_score}/100)] '{branch_name}' 브랜치가 모든 검증을 통과하여 "
                "main 브랜치 머지 대상으로 승인되었습니다."
            )
        else:
            feedback = (
                f"[REJECTED ({total_score}/100)] '{branch_name}' 브랜치에서 아래 문제를 수정한 뒤 "
                "다시 커밋하세요:\n" + "\n".join(failed_reasons)
            )

        return ReviewDecision(
            branch_name=branch_name,
            worker_id=worker_id,
            approved=approved,
            total_score=total_score,
            criteria=criteria,
            feedback_for_worker=feedback,
            reviewer_notes=llm_critique or feedback,
        )
