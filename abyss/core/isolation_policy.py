"""
================================================================================
[MY LEARNING CORE #2] abyss/core/isolation_policy.py
Claude Code 4-Check Git Worktree Isolation Policy
================================================================================

[구현 메커니즘 포인트]
병렬 서브에이전트가 동일한 작업 디렉토리(main checkout)를 동시에 수정하면
파일 덮어쓰기나 `.git/index.lock` 경합이 발생하고, 반대로 컨테이너를 매번 복제하면
오버헤드가 커집니다.

Claude Code의 "Run parallel sessions with worktrees" 아키텍처는
Git Worktree(`.abyss/worktrees/<agent_id>`)로 작업 트리를 가볍게 분리하되,
도구 실행 직전 아래 4가지 결정론적 검증(4-Check Enforcement)을 강제합니다:

  1. File Edit Check      : Edit/Write/Delete 대상 경로가 자신의 Worktree 내부인지 검증
                            (Main checkout 경로 및 `.git` 포인터 파일 직접 수정 즉시 차단)
  2. Command CWD Check    : Bash/Shell 명령의 실행 디렉토리(cwd)가 자신의 Worktree
                            외부이거나 Main checkout으로 해석되면 차단
  3. Git Redirect Check   : 명령어 텍스트에 `git -C`, `--git-dir`, `--work-tree`,
                            `GIT_DIR=`, `GIT_WORK_TREE=`, 또는 Worktree 외부로의
                            `cd` 후 git 실행이 포함되면 차단
  4. Command Shape Check  : `${!var}`, `eval`, 백틱(`` `cmd` ``), `git $(...)` 등 런타임에
                            명령어/경로가 동적으로 계산되어 정적 검증이 불가능한 형태를
                            차단하고, 평문 명령어로 쪼개서 다시 호출하도록 유도
================================================================================
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PolicyVerdict:
    """Worktree 격리 정책 검증 결과."""

    allowed: bool
    check_name: str  # "file_edit" | "command_cwd" | "git_redirect" | "command_shape"
    reason: str
    remediation_hint: str = ""


class WorktreeIsolationPolicy:
    """
    Claude Code 규격의 4대 Worktree 격리 정책 검증기.

    각 Worker 서브에이전트는 자신에게 할당된 `worktree_root` 내부에서만
    파일 수정과 셸/Git 명령을 수행할 수 있으며, `main_checkout_root`를 침범할 수 없습니다.
    """

    # Check 3: Git 리다이렉트 및 환경변수 우회 패턴
    _GIT_REDIRECT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"\bgit\s+-C\s+([^\s;|&]+)"),
            "git -C <path> 플래그를 통한 외부 디렉토리 Git 조작이 감지되었습니다.",
        ),
        (
            re.compile(r"--git-dir(?:\s*=|\s+)([^\s;|&]+)"),
            "--git-dir 플래그를 통한 .git 메타데이터 직접 접근은 금지됩니다.",
        ),
        (
            re.compile(r"--work-tree(?:\s*=|\s+)([^\s;|&]+)"),
            "--work-tree 플래그를 통한 작업 트리 우회는 금지됩니다.",
        ),
        (
            re.compile(r"\b(?:GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE)\s*="),
            "GIT_DIR / GIT_WORK_TREE 환경변수 재정의는 Worktree 격리를 우회하므로 차단됩니다.",
        ),
    )

    # Check 4: 정적 분석이 불가능한 동적 명령어 형태(Command Shape) 패턴
    _DYNAMIC_SHAPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"\$\{![a-zA-Z_][a-zA-Z0-9_]*\}"),
            "간접 변수 확장(${!name})은 실행 전 대상 경로를 검증할 수 없습니다.",
        ),
        (
            re.compile(r"\beval\s+"),
            "eval 명령은 런타임에 임의의 셸 구문을 실행하므로 Worktree 격리 하에서 금지됩니다.",
        ),
        (
            re.compile(r"`[^`]+`"),
            "백틱(`...`) 서브셸 치환은 정적 명령 형태 검증을 우회할 수 있습니다.",
        ),
        (
            re.compile(r"\bgit\s+\$\("),
            "git 명령 인자에 동적 명령 치환($(...))을 사용할 수 없습니다.",
        ),
    )

    def __init__(self, worktree_root: str | Path, main_checkout_root: str | Path) -> None:
        self.worktree_root = Path(worktree_root).resolve()
        self.main_checkout_root = Path(main_checkout_root).resolve()

    # =========================================================================
    # [CHECK 1] File edits: 블록/허용 여부 판정
    # =========================================================================
    def validate_file_edit(self, target_path: str | Path) -> PolicyVerdict:
        """
        [Claude Code Check #1: File Edits]
        Edit, Write, Delete 도구가 타겟팅하는 파일 경로가 현재 에이전트의
        `worktree_root` 내부에 있는지 검증합니다.
        심볼릭 링크나 `../` 상대 경로를 통해 `main_checkout_root` 또는 외부 경로를
        수정하려는 시도를 차단합니다.
        """
        raw_path = Path(target_path)
        if not raw_path.is_absolute():
            resolved = (self.worktree_root / raw_path).resolve()
        else:
            resolved = raw_path.resolve()

        # 1) 자신의 Worktree 내부 경로인지 확인
        if resolved.is_relative_to(self.worktree_root):
            # .git 포인터 파일 직접 덮어쓰기 방지 (Claude Code git identity 보호 규칙)
            if resolved == self.worktree_root / ".git":
                return PolicyVerdict(
                    allowed=False,
                    check_name="file_edit",
                    reason=f"Refusing to modify worktree git identity file: {resolved}",
                    remediation_hint="Worktree의 .git 메타데이터 파일은 직접 수정할 수 없습니다. 일반 git 명령을 사용하세요.",
                )
            return PolicyVerdict(
                allowed=True,
                check_name="file_edit",
                reason=f"File path is safely inside isolated worktree ({self.worktree_root.name}).",
            )

        # 2) Main Checkout 침범인지 구체적으로 안내
        if resolved.is_relative_to(self.main_checkout_root):
            return PolicyVerdict(
                allowed=False,
                check_name="file_edit",
                reason=(
                    f"Blocked write to main checkout path '{resolved}' from isolated worktree "
                    f"'{self.worktree_root}'."
                ),
                remediation_hint=(
                    f"현재 세션은 Worktree('{self.worktree_root}')에 격리되어 있습니다. "
                    f"상대 경로 또는 '{self.worktree_root}' 하위 경로만 수정하세요."
                ),
            )

        return PolicyVerdict(
            allowed=False,
            check_name="file_edit",
            reason=f"Blocked write outside isolated worktree: '{resolved}'.",
            remediation_hint=f"모든 파일 생성 및 수정은 '{self.worktree_root}' 내부에서만 허용됩니다.",
        )

    # =========================================================================
    # [CHECK 2] Command working directory: 명령 실행 위치 검증
    # =========================================================================
    def validate_command_cwd(self, cwd: str | Path | None) -> PolicyVerdict:
        """
        [Claude Code Check #2: Command Working Directory]
        Bash/Shell 명령의 작업 디렉토리(cwd)가 자신의 Worktree 내부로 해석되는지 검증합니다.
        """
        if cwd is None:
            resolved_cwd = self.worktree_root
        else:
            raw_cwd = Path(cwd)
            resolved_cwd = (
                (self.worktree_root / raw_cwd).resolve()
                if not raw_cwd.is_absolute()
                else raw_cwd.resolve()
            )

        if resolved_cwd.is_relative_to(self.worktree_root):
            return PolicyVerdict(
                allowed=True,
                check_name="command_cwd",
                reason=f"Command cwd '{resolved_cwd}' is within worktree.",
            )

        return PolicyVerdict(
            allowed=False,
            check_name="command_cwd",
            reason=(
                f"Blocked command execution: working directory '{resolved_cwd}' resolves outside "
                f"isolated worktree '{self.worktree_root}'."
            ),
            remediation_hint=f"명령 실행 위치(cwd)를 '{self.worktree_root}' 내부로 지정하세요.",
        )

    # =========================================================================
    # [CHECK 3] Git redirects: git -C, --git-dir, cd 우회 차단
    # =========================================================================
    def validate_git_redirect(self, command: str) -> PolicyVerdict:
        """
        [Claude Code Check #3: Git Redirects]
        셸 명령이 `git -C`, `--git-dir`, `GIT_DIR=`, 또는 `cd <main_checkout>`을 통해
        메인 체크아웃의 Git 상태를 변경하려는 시도를 차단합니다.
        """
        for pattern, description in self._GIT_REDIRECT_PATTERNS:
            match = pattern.search(command)
            if match:
                # git -C <path> 인 경우, 그 경로가 현재 worktree 내부인지 확인
                if match.lastindex and match.lastindex >= 1:
                    target_arg = match.group(1).strip("'\"")
                    candidate = (
                        (self.worktree_root / target_arg).resolve()
                        if not os.path.isabs(target_arg)
                        else Path(target_arg).resolve()
                    )
                    if candidate.is_relative_to(self.worktree_root):
                        continue
                return PolicyVerdict(
                    allowed=False,
                    check_name="git_redirect",
                    reason=f"Blocked git redirect outside worktree: {description}",
                    remediation_hint=(
                        "Worktree 외부로 git 명령을 리다이렉트하지 마세요. "
                        "현재 Worktree 루트에서 평문 git 명령(예: `git status`, `git commit`)을 실행하세요."
                    ),
                )

        # `cd <path>` 로 Worktree 밖으로 이동하는 패턴 검사
        cd_matches = re.finditer(r"\bcd\s+([^\s;&|]+)", command)
        for cd_match in cd_matches:
            cd_target = cd_match.group(1).strip("'\"")
            if cd_target in ("-", "~"):
                return PolicyVerdict(
                    allowed=False,
                    check_name="git_redirect",
                    reason=f"Blocked 'cd {cd_target}' which escapes isolated worktree '{self.worktree_root}'.",
                    remediation_hint="Worktree 외부 디렉토리로 cd 하지 마세요.",
                )
            resolved_cd = (
                (self.worktree_root / cd_target).resolve()
                if not os.path.isabs(cd_target)
                else Path(cd_target).resolve()
            )
            if not resolved_cd.is_relative_to(self.worktree_root):
                return PolicyVerdict(
                    allowed=False,
                    check_name="git_redirect",
                    reason=(
                        f"Blocked 'cd {cd_target}' resolving to '{resolved_cd}' outside worktree "
                        f"'{self.worktree_root}'."
                    ),
                    remediation_hint=f"cd 대상 경로는 반드시 '{self.worktree_root}' 하위여야 합니다.",
                )

        return PolicyVerdict(
            allowed=True,
            check_name="git_redirect",
            reason="No external git redirects or directory escapes detected.",
        )

    # =========================================================================
    # [CHECK 4] Command shape: 동적/난독화 명령 형태 차단
    # =========================================================================
    def validate_command_shape(self, command: str) -> PolicyVerdict:
        """
        [Claude Code Check #4: Command Shape]
        명령어 텍스트만 보고 안전성을 검증할 수 없는 동적 셸 구문(${!name}, eval, 백틱 등)을
        차단하고, 평문 명령어로 재작성하도록 안내합니다.
        """
        if not command or not command.strip():
            return PolicyVerdict(
                allowed=False,
                check_name="command_shape",
                reason="Empty command is not allowed.",
                remediation_hint="실행할 유효한 명령어를 입력하세요.",
            )

        for pattern, description in self._DYNAMIC_SHAPE_PATTERNS:
            if pattern.search(command):
                return PolicyVerdict(
                    allowed=False,
                    check_name="command_shape",
                    reason=f"Unverifiable command shape blocked: {description}",
                    remediation_hint=(
                        "Claude Code Worktree 격리 규칙에 따라 동적 명령 치환이나 eval 대신 "
                        "명확한 평문 명령어(plain, separate commands)로 나누어 다시 호출하세요."
                    ),
                )

        return PolicyVerdict(
            allowed=True,
            check_name="command_shape",
            reason="Command shape is statically verifiable.",
        )

    # =========================================================================
    # 통합 검증 진입점 (Bash / Shell 도구 호출 시 2~4번 연속 검증)
    # =========================================================================
    def validate_shell_command(
        self, command: str, cwd: str | Path | None = None
    ) -> PolicyVerdict:
        """셸 명령 실행 전 Check #2, #3, #4를 순차적으로 모두 수행합니다."""
        for check_fn in (
            lambda: self.validate_command_cwd(cwd),
            lambda: self.validate_command_shape(command),
            lambda: self.validate_git_redirect(command),
        ):
            verdict = check_fn()
            if not verdict.allowed:
                return verdict
        return PolicyVerdict(
            allowed=True,
            check_name="all_shell_checks",
            reason="Command passed CWD, Command Shape, and Git Redirect isolation checks.",
        )
