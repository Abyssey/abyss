# Claude Code 최신 아키텍처 변경점(`code.claude.com/docs`) 분석 및 Abyss 구현 가이드

본 문서는 공식 Claude Code 문서(`code.claude.com/docs`, v2.1.234 ~ v2.1.326+)를 기반으로 **최근 Claude Code 아키텍처에서 실제로 무엇이 달라졌는지**, 그리고 공식 바이너리의 사내 도입 제약을 극복하기 위해 **Abyss가 5대 코어 모듈(`abyss/core/`)을 어떻게 구현했는지** 정리한 AI 엔지니어용 기술 문서입니다.

---

## 1. 공식 문서(`code.claude.com/docs`) 기준 핵심 아키텍처 변경점 5가지

### ① Direct Tool Loop에서 Code Mode `REPL`로의 전환 (v2.1.323+, Agent Teams 지원 v2.1.326+)
- **변경 전**: 에이전트가 `Bash`, `Read`, `Edit`, `Grep`을 개별 tool call로 호출하고 매 호출의 원문 stdout을 컨텍스트에 적재한 뒤 다음 행동을 결정했습니다.
- **변경 후 (`CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1`)**:
  - 모델이 개별 도구를 호출하는 대신 **`REPL`이라는 단일 도구에 JavaScript 코드 블록을 작성**합니다(`await $`rg -l ...`.text()`, `await edit(...)`).
  - 한 번의 LLM 턴 안에서 **병렬 실행(`Promise.all` / `gather`)**, **이전 명령 출력 파싱 후 다음 도구 인자로 전달**, **모델이 읽어야 할 최종 표현식(Last Expression)만 필터링해 반환**합니다.
  - 대화창에는 실행된 코드 대신 내부 도구의 `Edit` diff만 노출되며, **`Ctrl+O` (Transcript Viewer)** 를 눌렀을 때만 접혀 있는 **Code** 행을 펼쳐 볼 수 있습니다.
  - 실행 시간 예산은 **Foreground 최대 10분**, 백그라운드 태스크 포함 시 **최대 2시간**으로 제한됩니다.

### ② Auto Mode 분류기(v2.1.283+)의 런타임 사각지대와 복합 명령 세그먼트 권한 검증
- **공식 문서의 핵심 보안 메커니즘 (`code.claude.com/docs/en/code-mode#permission-checks`)**:
  - v2.1.283+부터 기본 시작 모드가 된 `auto` 모드의 분류기(Classifier)는 **코드가 실행되기 전에 전체 코드 블록을 사전 검토**합니다.
  - 하지만 공식 문서는 명시적으로 다음과 같이 경고합니다:
    > *"Because it runs before the code, it cannot see values that the code computes while running, such as a path built from command output. If you need to block specific commands or paths regardless of how the code constructs them, use `deny` or `ask` rules, a `PreToolUse` hook, or sandboxing. Those checks run on each tool call inside the code with the resolved arguments."*
  - 또한 Code Mode 내부에서 실행되는 명령은 훅과 권한 엔진에 **`cd 'app' && git push` 같은 복합 명령(Compound Command)** 형태로 도달합니다.
- **Abyss 구현 ([`abyss/core/permissions.py`](../abyss/core/permissions.py), [`abyss/core/code_mode.py`](../abyss/core/code_mode.py))**:
  - **STEP 0 (`review_code` + `REPL` Hook)**: 실행 전 AST 정적 분석으로 `import`, `_dunder` 접근 및 금지 패턴을 1차 차단합니다.
  - **STEP 2 (`split_compound_command` + Per-Call Gate)**: 코드 실행 중 내부 도구(`sh`, `edit`, `write`, `read`)가 호출될 때마다 런타임에 확정된 인자(resolved arguments)를 대상으로 따옴표(`'...'`)와 리다이렉션(`2>&1`)을 보존하며 `&&`, `||`, `;`, `|` 세그먼트를 분리하고, 환경변수 prefix(`FOO=1 git push`)를 정규화한 뒤 `Bash(git push*)` 같은 `deny`/`ask` 규칙과 `PreToolUse` 훅(`the hook did not finish` 타임아웃 및 `onFailure: "block" | "continue"`)을 강제합니다.

### ③ 5대 병렬 실행 표면 구분과 Agent Teams + Git Worktree 하이브리드
- 공식 문서(`code.claude.com/docs/en/agents`, `agent-teams`)는 병렬 실행 표면을 5가지로 구분합니다:
  1. **Subagents**: 단일 세션 내 위임(부모에게만 결과 반환, `isolation: "worktree"` 선택 가능)
  2. **Agent view (`claude agents`)**: 독립 백그라운드 세션(자동 Git Worktree 격리)
  3. **Agent teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`)**: Lead + Teammates 구조로 **파일 락 기반 Shared Task List (`~/.claude/tasks/{team}/`, 선행 태스크 완료 시 의존 태스크 자동 unblock)**, **P2P Mailbox (`~/.claude/teams/{team}/inboxes/{agent}.json`)**, **품질 게이트 훅 (`TeammateIdle`, `TaskCreated`, `TaskCompleted`에서 종료 코드 2 반환 시 완료 차단 및 피드백 전송)**, **에이전트 간 비신뢰 메시지 마킹(다른 에이전트가 보낸 `SendMessage`로 사용자 권한 승인을 대리할 수 없음)** 을 제공합니다.
  4. **Dynamic workflows (`/workflows`)** & **5. Projects**
- **공식 `Agent Teams`의 한계와 Abyss의 하이브리드 설계**:
  - 공식 `agent-teams` 문서는 *"Agent teams don't isolate teammates in worktrees, so partition the work so each teammate owns a different set of files"* 라고 명시합니다.
  - **Abyss ([`abyss/orchestrator/team_coordinator.py`](../abyss/orchestrator/team_coordinator.py))** 는 **Agent Teams의 파일 락 태스크 보드 + P2P 메일박스 + 품질 게이트**에 **각 Teammate별 독립 Git Worktree(`.abyss/worktrees/<id>`)와 4대 경로 격리 정책([`isolation_policy.py`](../abyss/core/isolation_policy.py))** 을 결합하여 파일 충돌 없이 P2P 스키마 합의와 병렬 커밋·자동 머지를 수행합니다.

### ④ 서브에이전트·컨텍스트 라이프사이클 제어 (v2.1.257 ~ v2.1.296+)
- **공식 변경점**:
  - `CLAUDE_CODE_SUBAGENT_MODEL` 및 `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`(v2.1.257+)을 통한 역할별 모델 강제 할당
  - `autoCompactWindow`(v2.1.296+) 및 `subagentPromptCacheTtl: "1h"`를 통한 컨텍스트 한계 제어
- **Abyss 구현 ([`abyss/core/agent_runtime.py`](../abyss/core/agent_runtime.py), [`abyss/llm/provider_rotator.py`](../abyss/llm/provider_rotator.py))**:
  - `role_based` 전략으로 Lead/Reviewer와 Worker의 모델을 분리(예: Lead는 Gemini, Worker는 사내 `Qwen 3.8 Flash`)하고, 매 턴 `<think>` 추론 스트림을 분리 추출하며, 태스크 종료 시 대화 히스토리 전체를 넘기는 대신 구조화된 [`HandoffArtifact`](../abyss/core/agent_runtime.py)로 컨텍스트를 리셋해 인계합니다.

### ⑤ 공식 Code Mode의 엔터프라이즈 제약 돌파 (`"it only works directly with the Anthropic API"`)
- 공식 문서(`code.claude.com/docs/en/code-mode#code-mode-doesnt-start`)에 명시된 실행 불가 조건:
  - 커스텀 `ANTHROPIC_BASE_URL`, 사내 LLM 게이트웨이, Bedrock, Vertex, Foundry 사용 시 **`it only works directly with the Anthropic API`** 오류와 함께 비활성화됨
  - `apiKeyHelper`, OpenTelemetry(`CLAUDE_CODE_ENABLE_TELEMETRY`), `-p`(비대화형 플래그), Agent SDK에서 사용 불가
- **Abyss의 해결책**:
  - Code Mode `REPL` 컴파일러·실행기와 3단계 보안 게이트를 파이썬([`abyss/core/code_mode.py`](../abyss/core/code_mode.py))으로 직접 구현하여 **사내 OpenAI 호환 게이트웨이(`Qwen 3.8 Flash`)**, **Gemini**, **OpenAI** 및 **비대화형 REST API(`/api/code-mode/run`)** 어디서든 동일하게 Code Mode를 사용할 수 있습니다.

---

## 2. 직접 구현한 5대 코어 모듈 (`abyss/core/`) 상세

| 코어 모듈 | 구현 핵심 및 공식 스펙 매핑 |
| :--- | :--- |
| **[`abyss/core/code_mode.py`](../abyss/core/code_mode.py)** | • **STEP 0 (`review_code`)**: AST 기반 정적 검증 (`import`, `__import__`, `_dunder` 차단) 및 `PreToolUse` `REPL` 훅(`tool_input.code`) 실행<br>• **STEP 1 (`compile_program`)**: 코드의 마지막 표현식(Last Expression)을 자동 `return`으로 변환 + 루프/함수마다 예산 틱(`__abyss_tick__`) 주입<br>• **STEP 2 (`_RunContext.gate`)**: 내부 `sh`, `read`, `write`, `edit`, `ls`, `gather` 호출마다 원래 도구 이름(`Bash`, `Edit` 등)으로 권한·Worktree 격리·`PreToolUse` 훅(`the hook did not finish`, `onFailure: "block" \| "continue"`) 검증<br>• **STEP 3/4**: Foreground(10분)/Background(2시간) 타임아웃 및 대화창 diff vs `Ctrl+O` Transcript Code 분리 |
| **[`abyss/core/permissions.py`](../abyss/core/permissions.py)** | • **[`split_compound_command()`](../abyss/core/permissions.py)**: 작은/큰따옴표와 `2>&1` 리다이렉션을 보존하며 `&&`, `\|\|`, `;`, `\|` 복합 명령을 세그먼트로 분리<br>• **[`PermissionRules`](../abyss/core/permissions.py)**: `cd 'app' && FOO=1 git push`에서 환경변수 prefix를 벗기고 `Bash(git push*)` `deny` > `ask` > `allow` 규칙 매칭 (`deny_instead_of_prompting` 지원) |
| **[`abyss/core/isolation_policy.py`](../abyss/core/isolation_policy.py)** | • **Check 1 (`validate_file_edit`)**: `Path.resolve().is_relative_to(worktree_root)` 기반 심볼릭 링크 및 `../` 탈출 차단<br>• **Check 2 (`validate_command_cwd`)**: 실행 CWD의 Worktree 내부 여부 검증<br>• **Check 3 (`validate_git_redirect`)**: `git -C`, `--git-dir`, `--work-tree`, `cd ..` 공유 `.git` 오염 차단<br>• **Check 4 (`validate_command_shape`)**: `${!var}`, `eval`, 백틱 등 동적 셸 구문 차단 |
| **[`abyss/core/agent_runtime.py`](../abyss/core/agent_runtime.py)** | • `extract_thinking_and_content()`: `<think>...</think>` 추론 블록 실시간 분리<br>• `code_mode` 주입 시 8개 개별 도구 스키마를 단일 `REPL` 도구 스키마로 자동 교체<br>• [`HandoffArtifact`](../abyss/core/agent_runtime.py): 수정 파일·차단 횟수·도구 이력 요약 아티팩트 생성 |
| **[`abyss/core/evaluator_rubric.py`](../abyss/core/evaluator_rubric.py)** | • `TaskCompleted` 품질 게이트 역할: **pytest 결과(35점) + 격리 준수(25점) + diff 스코프(20점) + 메일박스 스키마 계약 및 anti-slop(20점)** 채점 후 80점 이상만 `git merge --no-ff` 승인 |

---

## 3. 라이브 데모 시연 순서

1. **탭 1 (`1. Core Architecture & Claude Code Diffs`)**
   - 공식 문서(`code.claude.com/docs`) 기준 5대 아키텍처 변경점 테이블과 우측 코드 뷰어에서 [`isolation_policy.py`](../abyss/core/isolation_policy.py), [`agent_runtime.py`](../abyss/core/agent_runtime.py), [`evaluator_rubric.py`](../abyss/core/evaluator_rubric.py), [`code_mode.py`](../abyss/core/code_mode.py), [`permissions.py`](../abyss/core/permissions.py) 실소스를 바로 확인합니다.
2. **탭 6 (`6. Code Mode (REPL · Ctrl+O) Lab`)**
   - **시나리오 1 (`retryPolicy` 1-Step 파이프라인)**: 공식 문서의 JS 예제(`$`rg ...``, `edit()`)와 Abyss Python Code Mode 코드를 비교 실행하여 LLM 왕복(4회 → 1회) 및 컨텍스트 절감률, `Edit` unified diff를 확인하고 **`Ctrl+O`** 로 Transcript Code 행을 토글합니다.
   - **시나리오 2 (`gather` 병렬 마크다운 링크 감사)**: 여러 파일을 단일 `REPL` 턴에서 병렬 검사하고 깨진 로컬 링크만 필터링해 반환합니다.
   - **시나리오 3 (`cd 'app' && git push` 복합 명령 차단)**: [`split_compound_command()`](../abyss/core/permissions.py)가 두 번째 세그먼트의 `git push`를 잡아내 `Bash(git push*)` 규칙으로 차단하는 것을 확인합니다.
   - **시나리오 4 (런타임 경로 조합 차단 & `PreToolUse` 훅 타임아웃)**: 런타임에 `"/".join(["..", "..", "outside_secret.py"])`로 만든 경로 탈출을 STEP 2가 차단하는 것과, `PreToolUse` 훅 타임아웃 체크 시 공식 스펙 그대로 `the hook did not finish`로 차단되는 것을 확인합니다.
3. **상단 `▶ Run Parallel Worktree Build & Merge` (탭 2 ~ 탭 5)**
   - 3개의 격리된 Git Worktree에서 Teammate들이 병렬로 `RepoOnboard`를 빌드하고, P2P Mailbox 합의 및 Evaluator 루브릭(100/100) 검증을 거쳐 `main`에 머지한 뒤, 탭 4(Socratic Q&A)와 탭 5(Archify 아키텍처 맵)에서 결과를 확인합니다.

---

## 4. 사내 `Qwen 3.8 Flash` 연결 및 실행

```bash
# 1. 환경 변수 설정 (.env)
cp .env.example .env
# QWEN_BASE_URL=http://<사내-vLLM-또는-게이트웨이>/v1
# QWEN_API_KEY=<사내-토큰>
# QWEN_MODEL=qwen-3.8-flash

# 2. 전체 테스트 스위트 검증 (15 passed)
.venv/bin/pytest -v

# 3. 웹 관제 서버 기동
.venv/bin/uvicorn abyss.web.server:app --host 0.0.0.0 --port 8000
```
