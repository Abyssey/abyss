"""
================================================================================
Focus Domain #1 · app/domains/guardrails/isolation_policy.py
Deterministic 4-Check Git Worktree Isolation Policy (Pure Python Stdlib)
================================================================================

[설계 의도]
병렬 코딩 에이전트가 동일한 작업 디렉토리(main checkout)를 동시에 수정하면
파일 덮어쓰기나 `.git/index.lock` 경합이 발생하고, 반대로 컨테이너를 매번 복제하면
오버헤드가 커집니다.

최신 코딩 에이전트 아키텍처(Claude Code Worktrees, Open SWE 격리 실행)는
Git Worktree(`.abyss/worktrees/<agent_id>`)로 작업 트리를 가볍게 분리하되,
도구 실행 직전 아래 4가지 결정론적 검증(4-Check Enforcement)을 코드로 강제합니다:

  1. File Edit Check      : Edit/Write/Delete 대상 경로가 자신의 Worktree 내부인지 검증
                            (심볼릭 링크·`../` 탈출, Main checkout 침범, `.git` 포인터 수정 차단)
  2. Command CWD Check    : Bash/Shell 명령의 실행 디렉토리(cwd)가 자신의 Worktree
                            외부이거나 Main checkout으로 해석되면 차단
  3. Git Redirect Check   : 명령어 텍스트에 `git -C`, `--git-dir`, `--work-tree`,
                            `GIT_DIR=`, `GIT_WORK_TREE=`, 또는 Worktree 외부로의
                            `cd` 후 git 실행이 포함되면 차단
  4. Command Shape Check  : `${!var}`, `eval`, 백틱(`` `cmd` ``), `git $(...)` 등 런타임에
                            명령어/경로가 동적으로 계산되어 정적 검증이 불가능한 형태를
                            차단하고, 평문 명령어로 나누어 호출하도록 유도
================================================================================
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from app.domains.guardrails.permissions import split_compound_command

_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPER_CMDS = frozenset({"env", "command", "builtin", "exec", "nohup"})
_GIT_OTHER_VAL_FLAGS = frozenset(
    {"--namespace", "--config-env", "--exec-path", "--super-prefix", "--list-cmds"}
)


def _strip_segment_grouping(segment: str) -> str:
    s = segment.strip()
    while s:
        changed = False
        if s.startswith("("):
            s = s[1:].strip()
            changed = True
        elif s == "{":
            return ""
        elif s.startswith("{") and len(s) > 1 and s[1].isspace():
            s = s[1:].strip()
            changed = True
        if not s:
            return ""
        if s in ("}", ")"):
            return ""
        if s.endswith("}") and len(s) > 1 and s[-2].isspace():
            s = s[:-1].strip()
            changed = True
        elif s.endswith(")") and (len(s) == 1 or s[-2] not in ("\\", '"', "'", "(")):
            s = s[:-1].strip()
            changed = True
        if not changed:
            break
    return s


@dataclass(frozen=True)
class PolicyVerdict:
    """Worktree 격리 정책 검증 결과."""

    allowed: bool
    check_name: str  # "file_edit" | "read_confinement" | "command_cwd" | "git_redirect" | "command_shape"
    reason: str
    remediation_hint: str = ""


class WorktreeIsolationPolicy:
    """
    4대 Worktree 격리 정책 검증기 (Pure Python Stdlib + `guardrails.permissions`).

    각 Worker/Builder 서브에이전트는 자신에게 할당된 `worktree_root` 내부에서만
    파일 수정과 셸/Git 명령을 수행할 수 있으며, `main_checkout_root`를 침범할 수 없습니다.
    """

    # Check 3: Git 리다이렉트 및 환경변수 우회 패턴 (모든 매치를 finditer로 전수 검사)
    _GIT_REDIRECT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"--git-dir(?:\s*=|\s+)([^\s;|&]+)"),
            "--git-dir 플래그를 통한 .git 메타데이터 직접 접근은 금지됩니다.",
        ),
        (
            re.compile(r"--work-tree(?:\s*=|\s+)([^\s;|&]+)"),
            "--work-tree 플래그를 통한 작업 트리 우회는 금지됩니다.",
        ),
        (
            re.compile(r"\b(?:GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_COMMON_DIR)\s*="),
            "GIT_DIR / GIT_WORK_TREE / GIT_COMMON_DIR 환경변수 재정의는 Worktree 격리를 우회하므로 차단됩니다.",
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
            re.compile(r"\$\("),
            "동적 명령 치환($(...))은 정적 검증을 우회할 수 있어 금지됩니다.",
        ),
        (
            re.compile(r"[<>]\("),
            "프로세스 치환(<(...)/ >(...))은 Worktree 격리 하에서 금지됩니다.",
        ),
    )

    def __init__(self, worktree_root: str | Path, main_checkout_root: str | Path) -> None:
        self.worktree_root = Path(worktree_root).resolve()
        self.main_checkout_root = Path(main_checkout_root).resolve()

    def _is_git_metadata_path(self, resolved: Path) -> bool:
        git_path = self.worktree_root / ".git"
        if resolved == git_path or resolved.is_relative_to(git_path):
            return True
        if resolved.is_relative_to(self.worktree_root):
            rel_parts = resolved.relative_to(self.worktree_root).parts
            if ".git" in rel_parts:
                return True
        return False

    def _is_path_inside_worktree(self, target_arg: str) -> bool:
        arg = target_arg.strip()
        if not arg or arg == "-" or arg.startswith("~") or "$" in arg or "\x00" in arg:
            return False
        candidate = (
            (self.worktree_root / arg).resolve()
            if not os.path.isabs(arg)
            else Path(arg).resolve()
        )
        if not candidate.is_relative_to(self.worktree_root):
            return False
        if self._is_git_metadata_path(candidate):
            return False
        return True

    # =========================================================================
    # [CHECK 1] File edits: 블록/허용 여부 판정
    # =========================================================================
    def validate_file_edit(self, target_path: str | Path) -> PolicyVerdict:
        """
        [Check #1: File Edits]
        Edit, Write, Delete 도구가 타겟팅하는 파일 경로가 현재 에이전트의
        `worktree_root` 내부에 있는지 검증합니다.
        심볼릭 링크·`../` 탈출, Main checkout 침범, `.git` 및 `.git/*` 수정 시도를 차단합니다.
        """
        raw_str = str(target_path)
        if not raw_str.strip() or "\x00" in raw_str:
            return PolicyVerdict(
                allowed=False,
                check_name="file_edit",
                reason="Empty or invalid file path is not allowed.",
                remediation_hint=f"유효한 상대 경로 또는 '{self.worktree_root}' 하위 경로를 지정하세요.",
            )

        raw_path = Path(raw_str)
        if not raw_path.is_absolute():
            resolved = (self.worktree_root / raw_path).resolve()
        else:
            resolved = raw_path.resolve()

        # 1) 자신의 Worktree 내부 경로인지 확인
        if resolved.is_relative_to(self.worktree_root):
            # .git 포인터 파일 및 .git/* 메타데이터 직접 수정 방지 (git identity 보호 규칙)
            if self._is_git_metadata_path(resolved):
                return PolicyVerdict(
                    allowed=False,
                    check_name="file_edit",
                    reason=f"Refusing to modify worktree git metadata path: {resolved}",
                    remediation_hint="Worktree의 .git 메타데이터 파일/디렉토리(.git/config, .git/hooks 등)는 직접 수정할 수 없습니다.",
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

    def validate_file_read(self, target_path: str | Path) -> PolicyVerdict:
        """[Check #1b: Read/List Confinement] 읽기/목록 조회 경로가 Worktree 내부인지 검증합니다."""
        raw_str = str(target_path)
        if not raw_str.strip() or "\x00" in raw_str:
            return PolicyVerdict(
                allowed=False,
                check_name="read_confinement",
                reason="Empty or invalid read path is not allowed.",
                remediation_hint=f"'{self.worktree_root}' 내부의 유효한 경로를 지정하세요.",
            )
        raw_path = Path(raw_str)
        resolved = (
            (self.worktree_root / raw_path).resolve()
            if not raw_path.is_absolute()
            else raw_path.resolve()
        )
        if not resolved.is_relative_to(self.worktree_root):
            return PolicyVerdict(
                allowed=False,
                check_name="read_confinement",
                reason=f"Read outside worktree: '{target_path}' -> '{resolved}'.",
                remediation_hint=f"파일 읽기 및 디렉토리 조회는 '{self.worktree_root}' 내부에서만 허용됩니다.",
            )
        if self._is_git_metadata_path(resolved):
            return PolicyVerdict(
                allowed=False,
                check_name="read_confinement",
                reason=f"Blocked read of git metadata path: '{resolved}'.",
                remediation_hint="Worktree 내부의 .git 메타데이터 직접 읽기는 허용되지 않습니다.",
            )
        return PolicyVerdict(
            allowed=True,
            check_name="read_confinement",
            reason=f"Read path '{resolved}' is confined within worktree.",
        )

    # =========================================================================
    # [CHECK 2] Command working directory: 명령 실행 위치 검증
    # =========================================================================
    def validate_command_cwd(self, cwd: str | Path | None) -> PolicyVerdict:
        """
        [Check #2: Command Working Directory]
        Bash/Shell 명령의 작업 디렉토리(cwd)가 자신의 Worktree 내부로 해석되는지 검증합니다.
        """
        if cwd is None:
            resolved_cwd = self.worktree_root
        else:
            raw_str = str(cwd)
            if "\x00" in raw_str:
                return PolicyVerdict(
                    allowed=False,
                    check_name="command_cwd",
                    reason="Null byte is not allowed in command working directory.",
                    remediation_hint=f"명령 실행 위치(cwd)를 '{self.worktree_root}' 내부로 지정하세요.",
                )
            raw_cwd = Path(raw_str).expanduser()
            resolved_cwd = (
                (self.worktree_root / raw_cwd).resolve()
                if not raw_cwd.is_absolute()
                else raw_cwd.resolve()
            )

        if resolved_cwd.is_relative_to(self.worktree_root) and not self._is_git_metadata_path(
            resolved_cwd
        ):
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
    # [CHECK 3] Git redirects: git -C, --git-dir, env -C, cd/pushd 우회 차단
    # =========================================================================
    def _reject_redirect(self, reason: str, hint: str | None = None) -> PolicyVerdict:
        return PolicyVerdict(
            allowed=False,
            check_name="git_redirect",
            reason=reason,
            remediation_hint=hint
            or (
                "Worktree 외부로 git 명령을 리다이렉트하거나 디렉토리를 벗어나지 마세요. "
                "현재 Worktree 루트에서 평문 git 명령(예: `git status`, `git commit`)을 실행하세요."
            ),
        )

    def _inspect_segment_redirects(self, segment: str) -> PolicyVerdict | None:
        cleaned = _strip_segment_grouping(segment)
        if not cleaned:
            return None

        try:
            tokens = shlex.split(cleaned, posix=True)
        except ValueError:
            # Conservative fallback on unbalanced quotes
            for match in re.finditer(r"\bgit\b[^;&|\n]*?(?:\s|^)-C(?:\s*([^\s;&|\n]+))", cleaned):
                arg = match.group(1).strip("'\"")
                if not self._is_path_inside_worktree(arg):
                    return self._reject_redirect(
                        f"Blocked git -C redirect outside worktree: '{arg}'."
                    )
            if re.search(r"\b(?:cd|pushd)\b", cleaned):
                return self._reject_redirect(
                    "Blocked cd/pushd in command with unbalanced quotes."
                )
            return None

        # Peel leading env assignments and wrappers while inspecting `env -C <dir>`
        cur = list(tokens)
        while cur:
            if _ENV_VAR_RE.match(cur[0]):
                cur.pop(0)
                continue
            head = posixpath.basename(cur[0]) if "/" in cur[0] else cur[0]
            if head not in _WRAPPER_CMDS:
                break
            if head == "env":
                idx = 1
                split_replaced = False
                while idx < len(cur):
                    tok = cur[idx]
                    if tok == "--":
                        idx += 1
                        break
                    if tok in ("-C", "--chdir"):
                        if idx + 1 >= len(cur) or not self._is_path_inside_worktree(cur[idx + 1]):
                            target = cur[idx + 1] if idx + 1 < len(cur) else "<missing>"
                            return self._reject_redirect(
                                f"Blocked 'env {tok} {target}' escaping worktree '{self.worktree_root}'."
                            )
                        idx += 2
                        continue
                    if tok.startswith("--chdir="):
                        target = tok.split("=", 1)[1]
                        if not self._is_path_inside_worktree(target):
                            return self._reject_redirect(
                                f"Blocked 'env {tok}' escaping worktree '{self.worktree_root}'."
                            )
                        idx += 1
                        continue
                    if tok.startswith("-C") and len(tok) > 2:
                        target = tok[2:]
                        if not self._is_path_inside_worktree(target):
                            return self._reject_redirect(
                                f"Blocked 'env {tok}' escaping worktree '{self.worktree_root}'."
                            )
                        idx += 1
                        continue
                    if tok in ("-S", "--split-string") and idx + 1 < len(cur):
                        try:
                            expanded = shlex.split(cur[idx + 1], posix=True)
                        except ValueError:
                            return self._reject_redirect(
                                "Blocked env -S with unparseable split-string."
                            )
                        cur = expanded + cur[idx + 2 :]
                        split_replaced = True
                        break
                    if tok in ("-u", "--unset"):
                        idx += 2
                        continue
                    if tok.startswith("-") or _ENV_VAR_RE.match(tok):
                        idx += 1
                        continue
                    break
                if split_replaced:
                    continue
                cur = cur[idx:]
                continue

            if head == "command":
                idx = 1
                while idx < len(cur) and cur[idx].startswith("-"):
                    tok = cur[idx]
                    idx += 1
                    if tok == "--":
                        break
                cur = cur[idx:]
                continue

            if head == "exec":
                idx = 1
                while idx < len(cur) and cur[idx].startswith("-"):
                    tok = cur[idx]
                    if tok == "--":
                        idx += 1
                        break
                    if tok == "-a" and idx + 1 < len(cur):
                        idx += 2
                    else:
                        idx += 1
                cur = cur[idx:]
                continue

            # builtin / nohup
            idx = 1
            if idx < len(cur) and cur[idx] == "--":
                idx += 1
            cur = cur[idx:]

        if not cur:
            return None

        cmd = posixpath.basename(cur[0]) if "/" in cur[0] else cur[0]

        if cmd == "git":
            idx = 1
            while idx < len(cur):
                tok = cur[idx]
                if tok == "--":
                    break
                if tok == "-C":
                    if idx + 1 >= len(cur) or not self._is_path_inside_worktree(cur[idx + 1]):
                        target = cur[idx + 1] if idx + 1 < len(cur) else "<missing>"
                        return self._reject_redirect(
                            f"Blocked git redirect outside worktree: git -C {target!r}."
                        )
                    idx += 2
                    continue
                if tok.startswith("-C") and len(tok) > 2:
                    target = tok[2:]
                    if not self._is_path_inside_worktree(target):
                        return self._reject_redirect(
                            f"Blocked git redirect outside worktree: git {tok!r}."
                        )
                    idx += 1
                    continue
                if tok in ("--git-dir", "--work-tree"):
                    if idx + 1 >= len(cur) or not self._is_path_inside_worktree(cur[idx + 1]):
                        target = cur[idx + 1] if idx + 1 < len(cur) else "<missing>"
                        return self._reject_redirect(
                            f"Blocked git redirect outside worktree: git {tok} {target!r}."
                        )
                    idx += 2
                    continue
                if tok.startswith(("--git-dir=", "--work-tree=")):
                    target = tok.split("=", 1)[1]
                    if not self._is_path_inside_worktree(target):
                        return self._reject_redirect(
                            f"Blocked git redirect outside worktree: git {tok!r}."
                        )
                    idx += 1
                    continue
                if tok == "-c":
                    if idx + 1 < len(cur):
                        kv_low = cur[idx + 1].strip().lower()
                        if kv_low.startswith(("core.worktree=", "core.gitdir=")):
                            return self._reject_redirect(
                                f"Blocked git config redirect: git -c {cur[idx + 1]!r}."
                            )
                    idx += 2
                    continue
                if tok.startswith("-c") and len(tok) > 2:
                    kv_low = tok[2:].strip().lower()
                    if kv_low.startswith(("core.worktree=", "core.gitdir=")):
                        return self._reject_redirect(
                            f"Blocked git config redirect: git {tok!r}."
                        )
                    idx += 1
                    continue
                if tok in _GIT_OTHER_VAL_FLAGS:
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                # Reached git subcommand (e.g., `diff -C HEAD` is safe)
                break
            return None

        if cmd in ("cd", "pushd"):
            targets: list[str] = []
            after_dd = False
            for tok in cur[1:]:
                if after_dd:
                    targets.append(tok)
                    continue
                if tok == "--":
                    after_dd = True
                    continue
                if tok == "-":
                    return self._reject_redirect(
                        f"Blocked '{cmd} -' which escapes isolated worktree '{self.worktree_root}'.",
                        "Worktree 외부 디렉토리로 이동하지 마세요.",
                    )
                if cmd == "pushd" and re.fullmatch(r"[+-]\d+", tok):
                    return self._reject_redirect(
                        f"Blocked 'pushd {tok}' directory stack rotation outside worktree.",
                        "Worktree 외부 디렉토리로 이동하지 마세요.",
                    )
                if tok.startswith("-") and set(tok[1:]).issubset({"L", "P", "e", "@", "n"}):
                    continue
                targets.append(tok)

            if not targets:
                return self._reject_redirect(
                    f"Blocked bare '{cmd}' which jumps to $HOME outside worktree '{self.worktree_root}'.",
                    "Worktree 외부 디렉토리로 이동하지 마세요.",
                )
            cd_target = targets[0]
            if not self._is_path_inside_worktree(cd_target):
                return self._reject_redirect(
                    f"Blocked '{cmd} {cd_target}' resolving outside worktree '{self.worktree_root}'.",
                    f"{cmd} 대상 경로는 반드시 '{self.worktree_root}' 하위여야 합니다.",
                )

        return None

    def validate_git_redirect(self, command: str) -> PolicyVerdict:
        """
        [Check #3: Git Redirects]
        셸 명령이 `git -C`, `--git-dir`, `--work-tree`, `git -c core.worktree=`,
        `GIT_DIR=`, `env -C`, 또는 Worktree 외부로의 `cd`/`pushd`를 통해
        메인 체크아웃의 Git 상태를 변경하려는 시도를 차단합니다.
        """
        for pattern, description in self._GIT_REDIRECT_PATTERNS:
            for match in pattern.finditer(command):
                if match.lastindex and match.lastindex >= 1:
                    target_arg = match.group(1).strip("'\"")
                    if self._is_path_inside_worktree(target_arg):
                        continue
                return self._reject_redirect(
                    f"Blocked git redirect outside worktree: {description}"
                )

        for seg in split_compound_command(command):
            verdict = self._inspect_segment_redirects(seg)
            if verdict is not None:
                return verdict

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
        [Check #4: Command Shape]
        명령어 텍스트만 보고 안전성을 검증할 수 없는 동적 셸 구문(${!name}, eval, 백틱, $(...), <(...)/ >(...))을
        차단하고, 평문 명령어로 재작성하도록 안내합니다.
        """
        if not command or not command.strip() or not command.strip(" \t\r\n;|&"):
            return PolicyVerdict(
                allowed=False,
                check_name="command_shape",
                reason="Empty or operator-only command is not allowed.",
                remediation_hint="실행할 유효한 명령어를 입력하세요.",
            )
        if "\x00" in command:
            return PolicyVerdict(
                allowed=False,
                check_name="command_shape",
                reason="Null byte is not allowed in shell commands.",
                remediation_hint="제어 문자 없이 평문 명령어를 입력하세요.",
            )

        for pattern, description in self._DYNAMIC_SHAPE_PATTERNS:
            if pattern.search(command):
                return PolicyVerdict(
                    allowed=False,
                    check_name="command_shape",
                    reason=f"Unverifiable command shape blocked: {description}",
                    remediation_hint=(
                        "Worktree 격리 규칙에 따라 동적 명령 치환이나 eval 대신 "
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
