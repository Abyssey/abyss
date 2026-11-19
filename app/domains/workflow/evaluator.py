"""
================================================================================
Workflow Domain · app/domains/workflow/evaluator.py
Independent Evaluator Rubric & Guided Diff Reviewer (Pure Python Stdlib Only)
================================================================================

[설계 의도 — 생성자(Worker/Builder)와 평가자(Reviewer)의 완전한 분리]
최신 자율 코딩 에이전트 아키텍처(Anthropic Three-Agent Harness, LangChain `open-swe` Reviewer &
`walkthrough` 가이드 diff 리뷰)는 코드를 작성하는 에이전트와 검증하는 에이전트를 엄격히 분리해
자기 과대평가(Self-evaluation bias)를 차단합니다.

이 순수 표준 라이브러리 모듈은 두 가지 독립 검증 게이트를 제공합니다:
  1. `evaluate_worktree_submission()` — 3-Worktree 병렬 빌드 100점 정량 머지 게이트
     (pytest 40 + git diff 실체 25 + 인터페이스/메일박스 계약 20 + 코드 완성도 15, >=80점 승인)
  2. `analyze_diff_for_review()` — Act 4 MR/브랜치 코드 리뷰 (`APPROVE` / `REQUEST_CHANGES`)
     유니파이드 diff 헝크(`file:line`)를 직접 파싱해 하드코딩된 비밀키, 예외 삼킴(`except: pass`),
     위험한 동적 실행(`eval`/`exec`), 미완성 스텁, 테스트 누락/실패를 탐지하고
     코딩 입문자도 바로 이해할 수 있는 한국어 원인 설명과 수정 제안을 반환합니다.
================================================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CriterionScore:
    """개별 평가 기준 채점 결과."""

    name: str
    score: int
    max_score: int
    passed: bool
    comment: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "max_score": self.max_score,
            "passed": self.passed,
            "comment": self.comment,
        }


@dataclass
class ReviewDecision:
    """3-Worktree 병렬 빌드에서 Reviewer 에이전트가 내리는 최종 머지 판정."""

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
            "criteria": [c.to_dict() for c in self.criteria],
            "feedback_for_worker": self.feedback_for_worker,
            "reviewer_notes": self.reviewer_notes,
        }


@dataclass
class ReviewFindingItem:
    """Act 4 Guided Diff Review (`open-swe` walkthrough 스타일)의 개별 `file:line` 지적 항목."""

    severity: Literal["high", "medium", "low"]
    file: str
    line: int
    comment: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "comment": self.comment,
            "suggestion": self.suggestion,
        }


@dataclass
class DiffStatsSummary:
    """Unified diff 통계 요약."""

    files_changed: int
    additions: int
    deletions: int
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_changed": self.files_changed,
            "additions": self.additions,
            "deletions": self.deletions,
            "files": list(self.files),
        }


@dataclass
class DiffReviewReport:
    """Act 4 독립 코드/MR 리뷰 최종 리포트."""

    decision: Literal["APPROVE", "REQUEST_CHANGES"]
    score: int
    summary: str
    findings: list[ReviewFindingItem] = field(default_factory=list)
    diff_stats: DiffStatsSummary = field(default_factory=lambda: DiffStatsSummary(0, 0, 0, []))
    rubric_breakdown: list[CriterionScore] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "score": self.score,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "diff_stats": self.diff_stats.to_dict(),
            "rubric_breakdown": [c.to_dict() for c in self.rubric_breakdown],
        }


class EvaluatorRubric:
    """
    독립 코드 리뷰 및 머지 게이트 루브릭 (Pure Python Stdlib).
    결정론적 정적/동적 검증(Diff, Pytest, Contract, Security Hygiene)을 수행합니다.
    """

    _STUB_PATTERN = re.compile(r"^\s*\+\s*(?:pass|raise NotImplementedError)\s*$", re.MULTILINE)
    _HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    # Act 4 정적 보안/품질 규칙 패턴
    _SECRET_ASSIGN_RE = re.compile(
        r"""\b[A-Za-z0-9_]*(?:API_SECRET|API_KEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY|DB_PASSWORD|PASSWORD|PASSWD|SECRET|TOKEN)\b(?:\s*:\s*[^=\n]+)?\s*=\s*["']([^"']{6,})["']""",
        re.IGNORECASE,
    )
    _SECRET_PLACEHOLDER_RE = re.compile(
        r"^(?:\*+|REDACTED|CHANGEME|CHANGE_ME|<[^>]+>|\$\{[^}]+\}|placeholder|example)$",
        re.IGNORECASE,
    )
    _SECRET_LITERAL_RE = re.compile(
        r"""["'](?:sk-[A-Za-z0-9_-]{6,}|glpat-[A-Za-z0-9_-]{6,}|AIza[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,})["']"""
    )
    _BARE_EXCEPT_PASS_INLINE_RE = re.compile(
        r"^\s*except(?:\s+Exception(?:\s+as\s+\w+)?)?\s*:\s*(?:pass\b|\.\.\.)"
    )
    _UNTYPED_BARE_EXCEPT_INLINE_RE = re.compile(
        r"^\s*except\s*:\s*(?!\s*(?:#.*)?$)(?!\s*(?:pass\b|\.\.\.)).+"
    )
    _UNTYPED_BARE_EXCEPT_HEADER_RE = re.compile(r"^\s*except\s*:\s*(?:#.*)?$")
    _BROAD_EXCEPT_HEADER_RE = re.compile(r"^\s*except\s+Exception(?:\s+as\s+\w+)?\s*:\s*(?:#.*)?$")
    _PASS_ONLY_RE = re.compile(r"^\s*(?:pass\b|\.\.\.)\s*(?:#.*)?$")
    _EVAL_EXEC_RE = re.compile(r"\b(?:eval|exec)\s*\(")
    _NOT_IMPL_RE = re.compile(r"^\s*raise\s+NotImplementedError\b")

    @staticmethod
    def _extract_pytest_failure_snippet(pytest_output: str, max_chars: int = 320) -> str:
        if not pytest_output:
            return ""
        lines = [ln.rstrip() for ln in pytest_output.splitlines() if ln.strip()]
        priority = [
            ln.lstrip()
            for ln in lines
            if ln.lstrip().startswith(("FAILED ", "ERROR "))
            or re.match(r"^\d+\s+failed\b", ln.lstrip())
        ]
        assertions = [
            ln.lstrip()
            for ln in lines
            if ln.lstrip().startswith(("E   ", "AssertionError"))
        ]
        if priority or assertions:
            return " | ".join(priority[:4] + assertions[:2])[:max_chars]
        filtered = [
            ln.strip()
            for ln in lines
            if "DeprecationWarning" not in ln and "warnings summary" not in ln.lower()
        ]
        fallback = " | ".join(filtered[-4:]) if filtered else pytest_output.strip()
        return fallback[-max_chars:]

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
        """Worker가 제출한 Worktree 브랜치를 4가지 객관적 기준(40/25/20/15=100)으로 채점합니다."""
        criteria: list[CriterionScore] = []
        required_symbols = required_symbols or []

        # 기준 1: Automated Test Verification (40점)
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
                    else f"pytest 실패 (exit_code={pytest_exit_code}): {self._extract_pytest_failure_snippet(pytest_output, 300)}"
                ),
            )
        )

        # 기준 2: Git Diff & Substance Check (25점)
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

        # 기준 3: Cross-Agent Schema & Symbol Contract (20점)
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

        # 기준 4: Craft & No Unimplemented Stubs (15점)
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

    @classmethod
    def parse_unified_diff(
        cls, git_diff: str
    ) -> tuple[DiffStatsSummary, list[tuple[str, int, str]]]:
        """
        Unified diff를 파싱하여 변경 파일 통계와 `(file_path, new_line_number, added_text)` 목록을 반환합니다.
        """
        files: list[str] = []
        current_file = "unknown"
        current_new_line = 0
        additions = 0
        deletions = 0
        added_entries: list[tuple[str, int, str]] = []

        for raw_line in git_diff.splitlines():
            if raw_line.startswith("+++ "):
                target = raw_line[4:].strip()
                if target != "/dev/null":
                    if target.startswith("b/"):
                        target = target[2:]
                    current_file = target
                    if current_file not in files:
                        files.append(current_file)
                continue
            if raw_line.startswith("--- ") or raw_line.startswith("diff --git "):
                if raw_line.startswith("diff --git "):
                    parts = raw_line.split()
                    if len(parts) >= 4:
                        candidate = parts[3].removeprefix("b/")
                        current_file = candidate
                        if current_file not in files:
                            files.append(current_file)
                continue

            hunk = cls._HUNK_HEADER_RE.match(raw_line)
            if hunk:
                current_new_line = int(hunk.group(1))
                continue

            if raw_line.startswith("+"):
                additions += 1
                line_no = max(1, current_new_line)
                added_entries.append((current_file, line_no, raw_line[1:]))
                current_new_line += 1
            elif raw_line.startswith("-"):
                deletions += 1
            elif raw_line.startswith(" ") or raw_line == "":
                if current_new_line > 0:
                    current_new_line += 1

        stats = DiffStatsSummary(
            files_changed=len(files),
            additions=additions,
            deletions=deletions,
            files=files,
        )
        return stats, added_entries

    @staticmethod
    def _is_test_file(file_path: str) -> bool:
        low = file_path.lower().replace("\\", "/")
        name = low.rsplit("/", 1)[-1]
        return low.startswith("tests/") or "/tests/" in low or name.startswith("test_")

    def analyze_diff_for_review(
        self,
        git_diff: str,
        *,
        pytest_exit_code: int | None = None,
        pytest_output: str = "",
        require_tests: bool = True,
    ) -> DiffReviewReport:
        """
        Act 4 독립 MR/브랜치 리뷰(`open-swe` Reviewer + `walkthrough` 가이드 diff 리뷰):
        - 하드코딩된 API 키/시크릿 탐지 (High, 접두사 포함 변수명·타입 어노테이션 지원)
        - 예외 삼킴 `except: pass` / `except Exception: pass` 및 untyped bare `except:` 탐지 (High)
        - 위험한 `eval()` / `exec()` 탐지 (High)
        - 미구현 스텁 `raise NotImplementedError` 탐지 (Medium)
        - 프로덕션 `.py` 변경 대비 단위 테스트 누락 탐지 (Medium)
        - `pytest` 실패 탐지 (High)
        """
        diff_stats, added_entries = self.parse_unified_diff(git_diff)
        findings: list[ReviewFindingItem] = []

        security_high_count = 0
        reliability_high_count = 0
        craft_issue_count = 0

        # (file_path, except_line, is_untyped_bare, except_indent, last_seen_line)
        pending_except: tuple[str, int, bool, int, int] | None = None

        def _emit_except_swallow(f_path: str, f_line: int) -> None:
            nonlocal reliability_high_count
            reliability_high_count += 1
            findings.append(
                ReviewFindingItem(
                    severity="high",
                    file=f_path,
                    line=f_line,
                    comment=(
                        "오류가 발생해도 아무 로그나 처리 없이 그냥 넘어가는 `except: pass` 패턴이 발견되었습니다. "
                        "실제 서비스 장애 원인이 완전히 감춰져 디버깅이 불가능해집니다."
                    ),
                    suggestion=(
                        "구체적인 예외 타입(예: `except ValueError as exc:`)만 잡고, "
                        "로깅(`logger.warning(...)`) 또는 안전한 에러 상태 응답을 반환하도록 수정하세요."
                    ),
                )
            )

        def _emit_untyped_bare_except(f_path: str, f_line: int) -> None:
            nonlocal reliability_high_count
            reliability_high_count += 1
            findings.append(
                ReviewFindingItem(
                    severity="high",
                    file=f_path,
                    line=f_line,
                    comment=(
                        "예외 타입을 지정하지 않은 `except:`(bare except) 구문이 발견되었습니다. "
                        "`KeyboardInterrupt`·`SystemExit`·`MemoryError`까지 모두 가로채 예기치 않은 오동작과 장애 은폐를 유발합니다."
                    ),
                    suggestion=(
                        "발생 가능한 구체적인 예외 타입(예: `except (ValueError, KeyError) as exc:`)을 명시하고 "
                        "원인을 로깅한 뒤 안전하게 처리하세요."
                    ),
                )
            )

        for file_path, line_no, text in added_entries:
            is_test = self._is_test_file(file_path)
            is_python = file_path.endswith(".py")
            stripped = text.strip()
            indent = len(text) - len(text.lstrip(" \t"))

            # 직전 줄(또는 공백/주석 이전)이 `except:` / `except Exception:` 헤더였던 경우 상태 전이
            if pending_except is not None:
                p_file, p_line, p_untyped, p_indent, p_last = pending_except
                contiguous = p_file == file_path and line_no == p_last + 1
                if contiguous and (not stripped or stripped.startswith("#")):
                    pending_except = (p_file, p_line, p_untyped, p_indent, line_no)
                    continue
                if contiguous and indent > p_indent and self._PASS_ONLY_RE.match(text):
                    _emit_except_swallow(p_file, p_line)
                    pending_except = None
                    continue
                if p_untyped:
                    _emit_untyped_bare_except(p_file, p_line)
                pending_except = None

            if not is_python or is_test or not stripped or stripped.startswith("#"):
                continue

            # 1) 하드코딩된 API 키 / 시크릿 검사
            assign_match = self._SECRET_ASSIGN_RE.search(text)
            if assign_match and self._SECRET_PLACEHOLDER_RE.match(assign_match.group(1).strip()):
                assign_match = None
            secret_match = assign_match or self._SECRET_LITERAL_RE.search(text)
            if secret_match and "os.getenv" not in text and "os.environ" not in text:
                security_high_count += 1
                findings.append(
                    ReviewFindingItem(
                        severity="high",
                        file=file_path,
                        line=line_no,
                        comment=(
                            "소스 코드에 API 비밀키/토큰이 평문으로 직접 적혀 있습니다(하드코딩). "
                            "Git 저장소에 커밋되면 사내/외부 보안 사고로 직결됩니다."
                        ),
                        suggestion=(
                            "비밀키는 코드에서 즉시 제거하고 `os.getenv('API_SECRET', '')`처럼 "
                            "환경변수(`.env` 또는 Pod Secret)에서 읽어오도록 변경하세요."
                        ),
                    )
                )

            # 2) 한 줄/여러 줄 `except: pass` 및 untyped bare `except:` 검사
            if self._BARE_EXCEPT_PASS_INLINE_RE.match(text):
                _emit_except_swallow(file_path, line_no)
            elif self._UNTYPED_BARE_EXCEPT_INLINE_RE.match(text):
                _emit_untyped_bare_except(file_path, line_no)
            elif self._UNTYPED_BARE_EXCEPT_HEADER_RE.match(text):
                pending_except = (file_path, line_no, True, indent, line_no)
            elif self._BROAD_EXCEPT_HEADER_RE.match(text):
                pending_except = (file_path, line_no, False, indent, line_no)

            # 3) eval / exec 동적 실행 검사
            if self._EVAL_EXEC_RE.search(text):
                security_high_count += 1
                findings.append(
                    ReviewFindingItem(
                        severity="high",
                        file=file_path,
                        line=line_no,
                        comment=(
                            "임의 문자열을 코드로 실행하는 `eval()`/`exec()` 호출이 포함되어 있어 "
                            "코드 주입(Code Injection) 보안 위험이 큽니다."
                        ),
                        suggestion="`json.loads()` 또는 명시적 분기 로직으로 안전한 파싱을 구현하세요.",
                    )
                )

            # 4) 미구현 스텁 검사
            if self._NOT_IMPL_RE.match(text):
                craft_issue_count += 1
                findings.append(
                    ReviewFindingItem(
                        severity="medium",
                        file=file_path,
                        line=line_no,
                        comment="아직 구현되지 않은 `raise NotImplementedError` 스텁 코드가 남아 있습니다.",
                        suggestion="실제 동작 로직을 완성한 뒤 단위 테스트로 검증하세요.",
                    )
                )

        if pending_except is not None and pending_except[2]:
            _emit_untyped_bare_except(pending_except[0], pending_except[1])

        # 5) 프로덕션 파이썬 파일 변경 대비 테스트 파일 누락 검사
        prod_py_files = [
            f for f in diff_stats.files if f.endswith(".py") and not self._is_test_file(f) and not f.endswith("__init__.py")
        ]
        test_files = [f for f in diff_stats.files if self._is_test_file(f)]
        missing_tests = bool(require_tests and prod_py_files and not test_files)
        if missing_tests:
            target_file = prod_py_files[0]
            stem = target_file.rsplit("/", 1)[-1].removesuffix(".py")
            findings.append(
                ReviewFindingItem(
                    severity="medium",
                    file=target_file,
                    line=1,
                    comment=(
                        f"새 기능/수정 코드(`{target_file}`)가 추가되었지만 이를 검증하는 자동화 단위 테스트(`tests/test_*.py`)가 "
                        "함께 포함되지 않았습니다."
                    ),
                    suggestion=(
                        f"`tests/test_{stem}.py` 테스트 파일을 추가하여 정상 케이스와 예외 케이스가 "
                        "`pytest`로 자동 검증되도록 보강하세요."
                    ),
                )
            )

        # 6) Pytest 실행 결과 검사
        pytest_failed = pytest_exit_code is not None and pytest_exit_code != 0
        if pytest_failed:
            target_file = test_files[0] if test_files else (diff_stats.files[0] if diff_stats.files else "tests/")
            snippet = self._extract_pytest_failure_snippet(pytest_output, max_chars=320)
            findings.append(
                ReviewFindingItem(
                    severity="high",
                    file=target_file,
                    line=1,
                    comment=f"격리 환경에서 실행한 `pytest`가 실패했습니다 (exit_code={pytest_exit_code}).",
                    suggestion=f"실패 로그를 확인하고 테스트가 100% 통과하도록 수정하세요: {snippet}",
                )
            )

        # 루브릭 정량 채점 (30 + 25 + 30 + 15 = 100점)
        sec_score = max(0, 30 - 25 * security_high_count)
        rel_score = max(0, 25 - 20 * reliability_high_count)
        if pytest_failed:
            test_score = 0
            test_passed = False
            test_comment = f"pytest 실패 (exit_code={pytest_exit_code})."
        elif missing_tests:
            test_score = 10
            test_passed = False
            test_comment = "프로덕션 코드 변경에 대응하는 단위 테스트 파일이 diff에 누락되었습니다."
        elif pytest_exit_code == 0:
            test_score = 30
            test_passed = True
            test_comment = "단위 테스트가 포함되어 있으며 격리 실행에서 모두 통과(exit_code=0)했습니다."
        else:
            test_score = 30
            test_passed = True
            test_comment = "변경 코드에 대응하는 테스트 파일이 포함되어 있습니다."

        has_diff = diff_stats.additions > 0
        craft_score = 15 if (has_diff and craft_issue_count == 0) else (5 if has_diff else 0)
        craft_passed = has_diff and craft_issue_count == 0

        rubric_breakdown = [
            CriterionScore(
                name="Security & Secret Hygiene",
                score=sec_score,
                max_score=30,
                passed=security_high_count == 0,
                comment=(
                    "하드코딩된 비밀키나 위험한 동적 코드 실행이 발견되지 않았습니다."
                    if security_high_count == 0
                    else f"보안 취약점 {security_high_count}건이 탐지되어 수정이 필요합니다."
                ),
            ),
            CriterionScore(
                name="Error Handling & Reliability",
                score=rel_score,
                max_score=25,
                passed=reliability_high_count == 0,
                comment=(
                    "예외를 조용히 삼키는 위험한 패턴이 없습니다."
                    if reliability_high_count == 0
                    else f"예외 처리 결함({reliability_high_count}건)으로 장애 원인이 숨겨질 위험이 있습니다."
                ),
            ),
            CriterionScore(
                name="Automated Test Coverage & Execution",
                score=test_score,
                max_score=30,
                passed=test_passed,
                comment=test_comment,
            ),
            CriterionScore(
                name="Diff Substance & Completeness",
                score=craft_score,
                max_score=15,
                passed=craft_passed,
                comment=(
                    f"총 {diff_stats.files_changed}개 파일(+{diff_stats.additions}/-{diff_stats.deletions})이 미완성 스텁 없이 cleanly 작성되었습니다."
                    if craft_passed
                    else "변경 내용이 비어 있거나 미구현 스텁이 포함되어 있습니다."
                ),
            ),
        ]

        total_score = sum(c.score for c in rubric_breakdown)
        has_high = any(f.severity == "high" for f in findings)
        decision: Literal["APPROVE", "REQUEST_CHANGES"] = (
            "APPROVE"
            if (not has_high and not pytest_failed and has_diff and total_score >= 80)
            else "REQUEST_CHANGES"
        )

        if decision == "APPROVE":
            summary = (
                f"승인(APPROVE · {total_score}/100): 보안 결함·예외 삼킴 없이 "
                f"{diff_stats.files_changed}개 파일(+{diff_stats.additions}/-{diff_stats.deletions})과 테스트가 깔끔하게 검증되었습니다."
            )
        else:
            high_cnt = sum(1 for f in findings if f.severity == "high")
            med_cnt = sum(1 for f in findings if f.severity == "medium")
            summary = (
                f"수정 요청(REQUEST_CHANGES · {total_score}/100): "
                f"심각(High) {high_cnt}건, 주의(Medium) {med_cnt}건이 발견되었습니다. "
                "아래 파일:라인별 수정 가이드를 반영한 뒤 다시 리뷰를 요청하세요."
            )

        return DiffReviewReport(
            decision=decision,
            score=total_score,
            summary=summary,
            findings=findings,
            diff_stats=diff_stats,
            rubric_breakdown=rubric_breakdown,
        )
