"""
================================================================================
Focus Domain #1 · app/domains/guardrails/permissions.py
Compound Shell Command Splitter & Permission Gate (`deny` > `ask` > `allow`)
================================================================================

[설계 의도 — 복합 명령 우회 차단]
Code Mode(`REPL`) 및 자율 코딩 에이전트 실행 중에는 `cd 'app' && FOO=1 git push origin main`
처럼 여러 셸 명령이 체인으로 묶여 단일 Bash 호출로 들어옵니다.

규칙이 명령 문자열 전체를 `git push*` 같은 단순 접두어로만 비교하면
`cd 'app' && git push`는 매칭되지 않아 그대로 통과하게 됩니다.
이 순수 표준 라이브러리 모듈은 **복합 명령을 세그먼트로 분리해 각 부분을 독립 검사**합니다:

  1) split_compound_command(): `&&`, `||`, `;`, `|`, 줄바꿈 기준 분리 (따옴표·`2>&1` 리다이렉션 보존)
  2) normalize_segment(): 앞쪽 환경변수 대입(`FOO=1 git push`) 제거
  3) PermissionRules.evaluate(): `Tool(pattern)` 문법의 `deny > ask > allow` 우선순위 판정

[보안 경계 원칙]
텍스트 기반 명령 규칙은 경로 기반 격리(`isolation_policy.py`) 및 컨테이너/OS 샌드박스와
함께 다층 방어(Defense-in-Depth)로 작동합니다.
================================================================================
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Literal

Decision = Literal["allow", "ask", "deny"]

_ENV_ASSIGN = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_]*=(?:"(?:\\.|[^"])*"|\'[^\']*\'|\S*)\s+)+')
_ENV_VAR_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RULE_SYNTAX = re.compile(r"^(?P<tool>[A-Za-z]+)(?:\((?P<pattern>.*)\))?$")
_WRAPPERS = frozenset({"env", "command", "builtin", "exec", "nohup"})
_GIT_GLOBAL_VAL_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--exec-path",
        "--super-prefix",
        "--list-cmds",
    }
)


def split_compound_command(command: str) -> list[str]:
    """
    `cd 'app' && git push; ls | wc -l` → ["cd 'app'", "git push", "ls", "wc -l"]

    따옴표('...', "...") 안의 연산자와 리다이렉션(`2>&1`, `&>file`)은 분리하지 않으며,
    POSIX 백슬래시 이스케이프(`\"`, `"\""`)를 정확히 처리합니다.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(command[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == '"':
                quote = None
        else:
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(command[i + 1])
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif command.startswith(("&&", "||", "|&"), i):
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            elif ch == "&" and (
                (i > 0 and command[i - 1] in "<>") or command.startswith("&>", i)
            ):
                buf.append(ch)
            elif ch in (";", "|", "\n", "&"):
                segments.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _strip_outer_grouping(segment: str) -> str:
    """Strip outer subshell `(...)` and brace-group `{ ...; }` delimiters from a segment."""
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


def _strip_git_global_flags(tokens: list[str]) -> list[str]:
    """Turn `git --no-pager -c k=v push origin main` into `git push origin main`."""
    if not tokens or tokens[0] != "git":
        return tokens
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok in _GIT_GLOBAL_VAL_FLAGS:
            i += 2
            continue
        if (tok.startswith("-C") or tok.startswith("-c")) and len(tok) > 2:
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    if i < n:
        return ["git", *tokens[i:]]
    return tokens


def _canonicalize_rm(tokens: list[str]) -> list[str]:
    """Canonicalize reordered/split `rm` recursive+force flags (`rm -fr /`, `rm -r -f /`) to `rm -rf ...`."""
    if not tokens or tokens[0] != "rm":
        return tokens
    has_r = False
    has_f = False
    other_flags: list[str] = []
    targets: list[str] = []
    after_dd = False

    for tok in tokens[1:]:
        if after_dd:
            targets.append(tok)
        elif tok == "--":
            after_dd = True
        elif tok == "--recursive":
            has_r = True
        elif tok == "--force":
            has_f = True
        elif tok.startswith("--"):
            other_flags.append(tok)
        elif tok.startswith("-") and len(tok) > 1:
            rem_chars: list[str] = []
            for ch in tok[1:]:
                if ch in ("r", "R"):
                    has_r = True
                elif ch == "f":
                    has_f = True
                else:
                    rem_chars.append(ch)
            if rem_chars:
                other_flags.append("-" + "".join(rem_chars))
        else:
            targets.append(tok)

    if not (has_r and has_f):
        return tokens

    abs_targets: list[str] = []
    rel_targets: list[str] = []
    for target in targets:
        if target.startswith("/"):
            norm_t = "/*" if target == "/*" else posixpath.normpath(target)
            if norm_t.startswith("//"):
                norm_t = "/" + norm_t.lstrip("/")
            abs_targets.append(norm_t)
        else:
            rel_targets.append(target)
    return ["rm", "-rf", *abs_targets, *other_flags, *rel_targets]


def _peel_env_and_wrappers(tokens: list[str]) -> list[str]:
    """Peel leading `VAR=val` assignments and standard shell wrappers (`env`/`command`/`builtin`/`exec`/`nohup`)."""
    cur = list(tokens)
    while cur:
        if _ENV_VAR_TOKEN.match(cur[0]):
            cur.pop(0)
            continue

        head = posixpath.basename(cur[0]) if "/" in cur[0] else cur[0]
        if head not in _WRAPPERS or len(cur) == 1:
            break

        if head == "env":
            idx = 1
            split_replaced = False
            while idx < len(cur):
                tok = cur[idx]
                if tok == "--":
                    idx += 1
                    break
                if tok in ("-S", "--split-string") and idx + 1 < len(cur):
                    arg = cur[idx + 1]
                    try:
                        expanded = shlex.split(arg, posix=True)
                    except ValueError:
                        expanded = arg.split()
                    cur = expanded + cur[idx + 2 :]
                    split_replaced = True
                    break
                if tok.startswith("--split-string="):
                    arg = tok.split("=", 1)[1]
                    try:
                        expanded = shlex.split(arg, posix=True)
                    except ValueError:
                        expanded = arg.split()
                    cur = expanded + cur[idx + 1 :]
                    split_replaced = True
                    break
                if tok.startswith("-S") and len(tok) > 2:
                    arg = tok[2:]
                    try:
                        expanded = shlex.split(arg, posix=True)
                    except ValueError:
                        expanded = arg.split()
                    cur = expanded + cur[idx + 1 :]
                    split_replaced = True
                    break
                if tok in ("-u", "-C", "--unset", "--chdir"):
                    idx += 2
                    continue
                if tok.startswith("-") or _ENV_VAR_TOKEN.match(tok):
                    idx += 1
                    continue
                break
            if split_replaced:
                continue
            if idx < len(cur):
                cur = cur[idx:]
                continue
            break

        if head == "command":
            idx = 1
            while idx < len(cur) and cur[idx].startswith("-"):
                tok = cur[idx]
                idx += 1
                if tok == "--":
                    break
            if idx < len(cur):
                cur = cur[idx:]
                continue
            break

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
            if idx < len(cur):
                cur = cur[idx:]
                continue
            break

        # builtin / nohup
        idx = 1
        if idx < len(cur) and cur[idx] == "--":
            idx += 1
        if idx < len(cur):
            cur = cur[idx:]
            continue
        break

    return cur


def normalize_segment(segment: str) -> str:
    r"""
    규칙 매칭용 셸 세그먼트 정규화:
      - 외부 그룹 `( ... )` / `{ ...; }` 제거
      - 따옴표 포함 환경변수 대입(`FOO=1 BAR="a && b"`) 및 래퍼(`env`/`command`/`builtin`/`exec`/`nohup`) 제거
      - 절대 실행 경로(`/usr/bin/git`), 백슬래시(`\git`), 따옴표(`'git'`), 다중 공백 정규화
      - `git` 전역 옵션 및 `rm` 재귀·강제 플래그(`rm -fr /`, `rm -r -f /`) 의미 정규화
    """
    cleaned = _strip_outer_grouping(segment)
    if not cleaned:
        return ""

    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        stripped = _ENV_ASSIGN.sub("", cleaned).strip()
        raw_parts = stripped.split()
        tokens = [p.lstrip("\\").strip("'\"") for p in raw_parts if p.lstrip("\\").strip("'\"")]

    tokens = _peel_env_and_wrappers(tokens)
    if not tokens:
        return ""

    if "/" in tokens[0]:
        tokens[0] = posixpath.basename(tokens[0])

    tokens = _strip_git_global_flags(tokens)
    tokens = _canonicalize_rm(tokens)
    return " ".join(tokens)


def _normalize_file_subject(path: str) -> str:
    """Normalize file tool paths (`sub/../.env` -> `.env`) while keeping outside absolute paths intact."""
    cleaned = path.strip().replace("\\", "/")
    if not cleaned:
        return ""
    norm = posixpath.normpath(cleaned)
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


@dataclass(frozen=True)
class PermissionDecision:
    decision: Decision
    rule: str = ""
    matched_segment: str = ""
    reason: str = ""


@dataclass
class PermissionRules:
    """
    `Tool(pattern)` 문법의 결정론적 권한 규칙 집합 (`deny > ask > allow`).

    예시:
      - "Bash(git push*)"   : 세그먼트가 `git push`로 시작하면 매칭
      - "Edit(.env)"        : `.env` 파일 수정 차단
      - "Read(secrets/*)"   : secrets 하위 읽기 차단
      - "Bash"              : 괄호 없이 쓰면 해당 도구 전체
    """

    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)

    @classmethod
    def default(cls) -> "PermissionRules":
        return cls(
            deny=[
                "Bash(git push*)",
                "Bash(rm -rf /*)",
                "Bash(curl *)",
                "Bash(wget *)",
                "Read(.env)",
                "Edit(.env)",
                "Write(.env)",
            ],
            ask=[
                "Bash(pip install*)",
                "Bash(npm install*)",
                "Bash(git reset --hard*)",
            ],
        )

    @staticmethod
    def _parse(rule: str) -> tuple[str, str | None]:
        m = _RULE_SYNTAX.match(rule.strip())
        if not m:
            raise ValueError(f"Invalid permission rule syntax: {rule!r}")
        return m.group("tool"), m.group("pattern")

    def _match(self, rules: list[str], tool: str, subjects: list[str]) -> tuple[str, str] | None:
        for rule in rules:
            rule_tool, pattern = self._parse(rule)
            if rule_tool != tool:
                continue
            if pattern is None:
                return rule, subjects[0] if subjects else ""
            for subject in subjects:
                if fnmatch.fnmatchcase(subject, pattern):
                    return rule, subject
        return None

    def evaluate(self, tool: str, subject: str) -> PermissionDecision:
        """
        tool="Bash"이면 subject(명령)를 세그먼트로 쪼개 **각 부분**을 검사합니다.
        파일 도구(Read/Edit/Write/Glob)는 경로를 정규화(`sub/../.env` -> `.env`)해 매칭합니다.
        """
        if tool == "Bash":
            subjects = [
                norm
                for seg in split_compound_command(subject)
                if (norm := normalize_segment(seg))
            ]
        else:
            subjects = [_normalize_file_subject(subject)]

        for decision, rules in (("deny", self.deny), ("ask", self.ask)):
            hit = self._match(rules, tool, subjects)
            if hit:
                rule, seg = hit
                return PermissionDecision(
                    decision=decision,  # type: ignore[arg-type]
                    rule=rule,
                    matched_segment=seg,
                    reason=f"{tool} segment {seg!r} matched {decision} rule {rule!r}",
                )
        return PermissionDecision(decision="allow", reason="No deny/ask rule matched.")
