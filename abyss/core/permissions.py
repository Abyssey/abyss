"""
================================================================================
[MY LEARNING CORE #4-b] abyss/core/permissions.py
Claude Code Permission Rules (deny / ask) + Compound Command Splitting
================================================================================

[왜 필요한가? — Code Mode 문서가 강조하는 복합 명령 우회 함정]
Code Mode에서는 모델이 쓴 코드 안의 tool call 하나하나가 평소 이름(Bash, Edit, ...)으로
`deny` / `ask` 규칙과 `PreToolUse` 훅을 통과해야 합니다. 그런데 문서는 이렇게 경고합니다:

    "In Code Mode, a `git push` in a folder named `app` can reach your hook as
     `cd 'app' && git push`."

즉 규칙이 명령 문자열 전체를 `git push*` 같은 접두어로만 비교하면
`cd 'app' && git push`는 매칭되지 않아 그대로 통과합니다.
그래서 이 모듈은 Bash 규칙이 하는 것처럼 **복합 명령을 세그먼트로 쪼개 각 부분을 검사**합니다.

  1) split_compound_command(): `&&`, `||`, `;`, `|`, 줄바꿈 기준 분리 (따옴표 내부는 보존)
  2) normalize_segment(): 앞쪽 환경변수 대입(`FOO=1 git push`) 제거
  3) PermissionRules.evaluate(): `Tool(pattern)` 문법의 deny → ask → allow 순서 판정

[한계 — 문서 원문 그대로]
"Bash rules match command text, so Claude can run a denied command in a form the rule
doesn't match." 텍스트 규칙은 우회 가능성이 남습니다. 명령 텍스트에 의존하지 않는 진짜 경계는
OS 샌드박스이며, Abyss에서는 isolation_policy.py(경로 기반 검증)가 그 역할 일부를 맡습니다.
================================================================================
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Literal

Decision = Literal["allow", "ask", "deny"]

_ENV_ASSIGN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_RULE_SYNTAX = re.compile(r"^(?P<tool>[A-Za-z]+)(?:\((?P<pattern>.*)\))?$")


def split_compound_command(command: str) -> list[str]:
    """
    `cd 'app' && git push; ls | wc -l` → ["cd 'app'", "git push", "ls", "wc -l"]

    따옴표('...', "...") 안의 연산자는 분리하지 않습니다.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif command.startswith(("&&", "||"), i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        elif ch == "&" and (
            (i > 0 and command[i - 1] in "<>") or command.startswith("&>", i)
        ):
            # `2>&1`, `&>file` 같은 리다이렉션은 명령 구분자가 아님
            buf.append(ch)
        elif ch in (";", "|", "\n", "&"):
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def normalize_segment(segment: str) -> str:
    """`FOO=1 BAR=2 git push` → `git push` (규칙 매칭용 정규화)."""
    return _ENV_ASSIGN.sub("", segment.strip()).strip()


@dataclass(frozen=True)
class PermissionDecision:
    decision: Decision
    rule: str = ""
    matched_segment: str = ""
    reason: str = ""


@dataclass
class PermissionRules:
    """
    Claude Code `settings.json`의 `permissions.deny` / `permissions.ask`를 흉내 낸 규칙 집합.

    규칙 문법: `Tool(pattern)` — pattern은 fnmatch 와일드카드
      - "Bash(git push*)"   : 세그먼트가 `git push`로 시작하면 매칭
      - "Edit(.env)"        : `.env` 파일 수정
      - "Read(secrets/*)"   : secrets 하위 읽기
      - "Bash"              : 괄호 없이 쓰면 해당 도구 전체
    판정 우선순위: deny > ask > allow (Claude Code와 동일)
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
        파일 도구(Read/Edit/Write)는 subject를 상대 경로로 보고 그대로 매칭합니다.
        """
        if tool == "Bash":
            subjects = [normalize_segment(s) for s in split_compound_command(subject)]
        else:
            subjects = [subject[2:] if subject.startswith("./") else subject]

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
