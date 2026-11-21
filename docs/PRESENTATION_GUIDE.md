# Modern Coding Agent Architecture 발표 & 라이브 데모 가이드

본 문서는 업계 최신 코딩 에이전트 아키텍처의 공통 수렴 원리와 **`langchain-ai/deepagents` 단일 엔진 + Modern Python DDD Modular Monolith (`app/`)** 설계 근거, 그리고 **4-Act 라이브 시연 스크립트** 를 정리한 기술 가이드입니다.

---

## 1. 아키텍처 설계 관점: 왜 이 구조인가?

### 1.1 업계 최신 코딩 에이전트의 공통 수렴 패턴
프로덕션급 코딩 에이전트 — **LangChain `deepagents` / `open-swe`**, **Stripe Minions**, **Coinbase Cloudbot**, **Claude Code**, **Antigravity** — 는 다음 설계 원칙으로 수렴하고 있습니다:

1. **격리된 실행 환경 (Isolated Git Worktree / Sandbox)**: 에이전트가 `main` 브랜치나 개발자의 로컬 작업 폴더를 직접 건드리지 않고, 태스크별 독립 브랜치(`agent/task-*`)와 Worktree에서 개발·테스트합니다.
2. **계획 → 승인 → 실행 → 검증 산출물 (Antigravity 3-Phase Artifacts)**: 에이전트가 곧바로 코드를 덮어쓰지 않고 먼저 `implementation_plan.md` 를 작성해 검토(`request_review` Proceed 게이트)를 거친 뒤, `task.md` 체크리스트를 갱신하며 코딩하고, 실제 `pytest` 출력이 스탬프된 `walkthrough.md` 로 결과를 증명합니다.
3. **작성자와 분리된 독립 읽기 전용 리뷰어**: Builder와 분리된 읽기 전용 Reviewer(`ReadOnlyGuardedBackend` + `ReviewFindings` 구조화 출력 + `EvaluatorRubric`)가 `file:line` 단위로 보안 취약점·예외 처리 누락·테스트 부재를 지적하고 `REQUEST_CHANGES` / `APPROVE` 를 결정합니다.
4. **결정론적 가드레일과 이중 완료 게이트**: 도구 실행 직전에 코드 레벨 정책으로 위험 명령과 경로 탈출을 차단하고, 완료 시점에 결정론적 `PytestGateMiddleware` 와 `RubricMiddleware` 를 모두 통과해야만 커밋합니다.

### 1.2 본 프로젝트의 핵심 차별점 (2대 Focus Domains)
- **★ Focus Domain #1 — `app/domains/guardrails/` (순수 표준 라이브러리, 외부 의존성 0개)**:
  - [`app/domains/guardrails/isolation_policy.py`](../app/domains/guardrails/isolation_policy.py): ① 파일 경로 검증(`Path.resolve().is_relative_to`, 심볼릭 링크·`../` 차단) ② 실행 CWD 검증 ③ Git Redirect(`git -C`, `--git-dir`, `cd ..`) 차단 ④ 동적 셸 구문(`${!var}`, `eval`, 백틱) 차단의 4-Check 결정론적 게이트.
  - [`app/domains/guardrails/permissions.py`](../app/domains/guardrails/permissions.py): `cd 'app' && FOO=1 git push origin main` 같은 복합 셸 명령을 세그먼트로 분리하고 환경변수 접두어를 정규화한 뒤 `deny > ask > allow` 규칙으로 차단.
- **★ Focus Domain #2 — `app/domains/execution/` (`langchain-ai/deepagents` 단일 런타임 + Code Mode `REPL`)**:
  - [`app/domains/execution/deepagent_backend.py`](../app/domains/execution/deepagent_backend.py): 모든 `deepagents` 내장 파일·셸 도구 앞에 4-Check 가드레일과 시크릿 스크러빙을 배치(`GuardedWorktreeBackend` / `ReadOnlyGuardedBackend`).
  - [`app/domains/execution/artifact_workflow.py`](../app/domains/execution/artifact_workflow.py): PLANNING 단계 코드 수정 차단(`[PLAN-FIRST REFUSAL]`), LangGraph `interrupt()` 기반 Plan Proceed 승인, `write_todos` → `task.md` 렌더링, `walkthrough.md` 검증 스탬프 강제.
  - [`app/domains/execution/code_mode.py`](../app/domains/execution/code_mode.py) & [`runtime.py`](../app/domains/execution/runtime.py): `create_deep_agent` 위에서 개별 도구 호출(`direct`)과 단일 비동기 `run_python`(`repl`) 배치 실행을 모두 지원합니다.

### 1.3 정직한 Fail-Fast 정책 (HTTP 503)
- 가짜 `mock` 응답기나 하드코딩된 결과물 템플릿을 두지 않습니다.
- 유효한 LLM API 키(`GEMINI_API_KEY`, `QWEN_API_KEY`, `OPENAI_API_KEY`)가 설정되지 않은 상태에서 에이전트 엔드포인트를 호출하면 즉시 **HTTP 503 (`⛔ LLM CONFIG REQUIRED`)** 를 반환하고 상단 배너로 키 설정을 안내합니다.
- 반면 결정론적 AST 분석인 **Act 1 (`/api/repo-onboard/*`)** 과 샌드박스 가드레일 검증인 **Act 3 (`POST /api/code-mode/run`)** 는 오프라인에서도 즉시 동작합니다.

---

## 2. 4-Act 라이브 발표 진행 스크립트 (약 10분)

발표 전 준비 (`.env` 에 `GEMINI_API_KEY` 또는 사내 `QWEN_API_KEY` 설정 후 서버 기동):
```bash
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/readyz
```
브라우저에서 `http://localhost:8000` 을 열고 상단 배지가 `langchain-ai/deepagents v0.7` 및 `🟢 LIVE LLM` 인지 확인한 뒤 **4-Act Story Ribbon** 을 따라 진행합니다.

### Act 1 · 낯선 프로젝트 구조 파악 & 온보딩 (2분)
- **1줄 프롬프트**: `"이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘"`
- **시연 동작**: **Tab 4** 에서 `내 로컬 개발 폴더 (self)` 로 AST 분석 실행 → 핵심 파일 읽기 순서와 3단계 소크라테스 문답 확인 → **Tab 5 (`Archify Architecture Map`)** 에서 골드 테두리로 강조된 `guardrails` · `execution` 도메인 의존성 확인.

### Act 2 · 격리 Worktree 자율 빌드 + Plan Proceed 승인 + 라이브 프리뷰 (3분)
- **1줄 프롬프트**: `"이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"`
- **시연 동작**:
  1. 좌측 **Mode 1 (`Act 2 · 만들기`)** 에서 기본 `request_review` 정책으로 **"▶ 격리 Worktree에서 만들기"** 실행.
  2. **Tab 2** 상단 Artifact 패널에 생성된 `1. implementation_plan.md` 카드를 확인하고(원하면 인라인 코멘트 입력 후) **✓ Proceed (실행 시작)** 클릭.
  3. 에이전트가 코딩과 `PytestGateMiddleware` · `RubricMiddleware` 검증을 마치고 `2. task.md` 와 `3. walkthrough.md` 를 완성하면, 우측 **`<iframe sandbox="allow-scripts">` 라이브 프리뷰** 에서 생성된 웹앱을 즉시 클릭해 시연합니다.

### Act 3 · 결정론적 보안 가드레일 차단 & Code Mode 에이전트 (2분)
- **1줄 프롬프트**: `"에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘"`
- **시연 동작**:
  1. **Tab 6** 의 자연어 Code Mode 에이전트(`POST /api/code-mode/agent`)에서 `repl` / `direct` 모드로 샌드박스 수정·테스트 실행.
  2. 하단 시나리오 3(`cd 'app' && FOO=1 git push origin main`)과 시나리오 4(`../../.env` 런타임 경로 조합 탈출)를 실행해 `status: "refused"` 로 결정론적 차단되는 과정을 시연합니다.

### Act 4 · 독립 읽기 전용 코드 리뷰어 (2분)
- **1줄 프롬프트**: `"방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘"`
- **시연 동작**:
  1. **Tab 3** 에서 **4-A: 결함 코드 리뷰** 실행 → `ReadOnlyGuardedBackend` 기반 Reviewer가 하드코딩 API 키·빈 `except: pass`·테스트 누락을 찾아 `REQUEST_CHANGES` 판정.
  2. **4-B: Act 2 브랜치 리뷰** 실행 → 방금 빌드한 `agent/task-*` 브랜치를 무변경(`read_only_verified: true`)으로 검증해 `APPROVE` 판정 확인.

---

## 3. 터미널 검증 명령어 요약

```bash
# 1) 오프라인 전체 테스트 스위트 (단위 · AST 경계 · 가드레일 · Scripted DeepAgent 그래프)
GEMINI_ENABLED=false QWEN_ENABLED=false OPENAI_ENABLED=false .venv/bin/pytest -q

# 2) 실제 Gemini API 기반 자가 수정 루프 검증 (.env에 GEMINI_API_KEY 설정 후)
.venv/bin/pytest -m live_llm -v

# 3) 라이브 서버 대상 4-Act 전체 엔드투엔드 스크립트 (Plan 자동 승인 포함)
bash scripts/demo.sh
```
