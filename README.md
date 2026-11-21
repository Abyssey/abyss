# Modern Coding Agent Architecture — `langchain-ai/deepagents` Single Engine, Guardrails, Code Mode (`REPL`) & Isolated Git Worktrees

최신 코딩 에이전트 아키텍처(LangChain [`deepagents`](https://github.com/langchain-ai/deepagents) · [`open-swe`](https://github.com/langchain-ai/open-swe), Stripe Minions, Coinbase Cloudbot, Claude Code, Antigravity)의 핵심 설계 패턴을 **Modern Python DDD Modular Monolith + FastAPI (`app/`)** 로 구현한 실전형 코딩 에이전트 하네스 및 웹 관제 플레인입니다.

- **단일 통합 에이전트 엔진 (`langchain-ai/deepagents v0.7`)**: 모든 에이전트 실행(`builder`, `worker`, `reviewer`, `direct`, `repl`)이 `create_deep_agent` 기반 [`DeepAgentRuntime`](app/domains/execution/runtime.py) 단일 엔진으로 구동됩니다. 가짜 `mock` 시뮬레이터나 하드코딩 템플릿이 없으며, LLM 자격증명이 없으면 즉시 **HTTP 503 (`⛔ LLM CONFIG REQUIRED`)** 로 Fail-Fast 합니다(오프라인 Act 1 구조 분석과 Act 3 REPL 가드레일 시나리오는 키 없이도 동작).
- **Antigravity 차용 3단계 산출물 워크플로우 (`PLANNING → EXECUTION → VERIFICATION`)**: [`ArtifactWorkflowMiddleware`](app/domains/execution/artifact_workflow.py) 와 `CompositeBackend` 가 Worktree 외부 가상 경로(`/artifacts/`)에 `implementation_plan.md`(기본 `request_review` 승인 게이트 + 코멘트 피드백), `task.md`(`write_todos` 결정적 동기화), `walkthrough.md`(`pytest` 실출력 조작 방지 스탬프)를 남기고 커밋·MR 설명으로 연결합니다.
- **2대 Focus Domains (핵심 차별점)**:
  1. **★ Focus Domain #1 (`app/domains/guardrails/`)**: 외부 의존성이 0개인 순수 Python 표준 라이브러리로 **4-Check Git Worktree 격리**와 **복합 셸 명령 세그먼트 권한 게이트(`deny > ask > allow`)** 를 결정론적으로 강제합니다.
  2. **★ Focus Domain #2 (`app/domains/execution/`)**: `GuardedWorktreeBackend` / `ReadOnlyGuardedBackend` 로 모든 `deepagents` 내장 도구를 보호하고, 이중 완료 게이트(`PytestGateMiddleware` + `RubricMiddleware`), 읽기 전용 서브에이전트(`repo-explorer`, `code-reviewer`), 그리고 단일 `run_python`(`REPL`) 배치 모드를 결합합니다.
- **로컬 폴더 우선 테스트 + 사내 Pod / Self-Hosted GitLab 배포 대응**: Git 초기화 전 일반 로컬 폴더(`/path/to/my-project`, `self`)를 원본 파일 손상 없이 분석·빌드할 수 있고, 사내 폐쇄망 Pod에서 Private GCP VM의 **Self-Hosted GitLab** 에 토큰 무노출(`GIT_CONFIG_COUNT` 호스트 범위 인증 헤더)로 안전하게 연결합니다.

---

## Modern Python DDD Modular Monolith 디렉토리 구조 (`app/`)

```text
app/
├── main.py                                   # Composition Root: create_app(), HTTP 503 LLM 핸들러, /healthz, /readyz
├── core/                                     # Shared Kernel (공통 설정·보안 Git 리졸버·DI·멀티 LLM 팩토리)
│   ├── config.py                             #   AppSettings (로컬 / Pod / 사내 GitLab / TLS 설정)
│   ├── git_source.py                         #   InternalGitSourceResolver (GIT_CONFIG_COUNT 무노출 인증 · 마스킹)
│   ├── container.py                          #   AppContainer & FastAPI Depends() 프로바이더 (model_factory 주입)
│   └── llm/
│       ├── provider_rotator.py               #   Gemini / Qwen 3.8 Flash / OpenAI LangChain 모델 팩토리 + ModelFallbackMiddleware
│       └── router.py                         #   /api/providers/*
├── domains/                                  # Bounded Contexts (도메인별 수직 슬라이스)
│   ├── guardrails/                           # ★ Focus Domain #1 (Pure Python Stdlib · 외부 의존성 0개 · router 없음)
│   │   ├── isolation_policy.py               #   4-Check Worktree 경로·CWD·Git Redirect·동적 셸 검증
│   │   └── permissions.py                    #   복합 셸 명령 세그먼트 분리기 & deny > ask > allow 엔진
│   ├── execution/                            # ★ Focus Domain #2 (deepagents 단일 런타임 · GuardedBackend · Code Mode REPL)
│   │   ├── code_mode.py                      #   Code Mode (REPL) 순수 Stdlib AST 정적 검토 + 호출별 권한/격리/Hook 게이트
│   │   ├── deepagent_backend.py              #   GuardedWorktreeBackend & ReadOnlyGuardedBackend (4-Check + 시크릿 스크러빙)
│   │   ├── artifact_workflow.py              #   ArtifactWorkflowMiddleware & PlanGateMiddleware (3단계 산출물 + interrupt 승인)
│   │   ├── runtime.py                        #   DeepAgentRuntime (create_deep_agent + PytestGate + RubricMiddleware + SubAgents)
│   │   ├── service.py                        #   Code Mode 시나리오(/run) & 자연어 DeepAgent(/agent: direct | repl)
│   │   ├── schemas.py                        #   Pydantic DTO
│   │   └── router.py                         #   /api/code-mode/{scenarios,run,agent}
│   ├── workflow/                             # 빌드·프리뷰·리뷰·멀티 에이전트 팀 조율 도메인
│   │   ├── evaluator.py                      #   Pure Stdlib 독립 품질 게이트(100점), Diff 리뷰어 & merge_llm_findings
│   │   ├── task_service.py                   #   Act 2 자율 빌드 + Plan Approve + Live Preview + Act 4 읽기 전용 리뷰
│   │   ├── coordinator.py                    #   Lead TeamPlan 분해 + 3-Worktree 병렬 Worker + Mailbox + 순차 Merge
│   │   ├── worktree_adapter.py               #   Git Worktree 생성·Lock·Merge 및 Safe Push 어댑터
│   │   ├── catalog_service.py                #   핵심 도메인 실소스 카탈로그 서비스 (/api/learning-core)
│   │   ├── schemas.py                        #   Build / PlanApprove / Review / Workflow Pydantic DTO
│   │   └── router.py                         #   /, /api/state, /api/workflow/*, /api/events/stream, /api/learning-core
│   └── onboarding/                           # 독립 온보딩 도메인 (다른 domains/* import 0개)
│       ├── git_source.py                     #   Core Git 리졸버 연동
│       ├── templates.py                      #   AST 저장소 분석기 (Archify IR) & 3단계 소크라테스 튜터
│       ├── service.py                        #   로컬 폴더(Git/비-Git) & 사내 GitLab 분석 서비스
│       ├── schemas.py                        #   Pydantic DTO
│       └── router.py                         #   /api/repo-onboard/*
└── static/                                   # Air-Gap Meta Astryx 6-탭 웹 관제 UI (Artifact 카드 + Plan Proceed 포함)
```

### AST 기반 아키텍처 경계 검증 (`tests/test_ddd_architecture.py`)
1. `app/domains/guardrails/*.py`: **100% Python 표준 라이브러리 + 내부 패키지** 만 허용 (HTTP 라우터 없음).
2. `app/domains/execution/code_mode.py`: 표준 라이브러리 + `guardrails` 만 허용. `runtime.py`, `deepagent_backend.py`, `artifact_workflow.py` 는 `deepagents`/`langchain*`/`langgraph`/`pydantic` 만 허용하며 `workflow`·`onboarding`·`app.core`·`fastapi` 역참조 금지.
3. `app/domains/workflow/evaluator.py`: **100% Python 표준 라이브러리** 만 허용.
4. `app/domains/onboarding/*.py`: `workflow`, `execution`, `guardrails`, `core.llm`, `app.main` 교차 import 금지.
5. 모든 `router.py`: `fastapi`, `starlette`, `pydantic`, `app.core.container`, 자기 도메인 패키지 외 import 금지.
6. 단일 엔진 불변식: `runtime.py` 가 `create_deep_agent` 와 `ArtifactWorkflowMiddleware` 를 직접 호출하며, `app/` 전체에 수동 `while` 루프·`mock_responder`·하드코딩 시드 템플릿이 존재하지 않음을 AST로 강제합니다.

---

## Fail-Fast LLM 정책 (HTTP 503) & Antigravity 3단계 산출물

- **HTTP 503 `⛔ LLM CONFIG REQUIRED`**: `GEMINI_API_KEY`·`QWEN_API_KEY`·`OPENAI_API_KEY` 중 유효한 자격증명이 하나도 없으면 `/api/workflow/tasks/build`, `/api/workflow/tasks/review`, `/api/workflow/run`, `/api/code-mode/agent` 는 가짜 응답 대신 즉시 **HTTP 503** 과 설정 가이드를 반환합니다.
- **Plan Review 정책 (`AGENT_PLAN_REVIEW`, 기본값 `request_review`)**:
  - `request_review`: 빌더 에이전트가 `/artifacts/implementation_plan.md` 를 작성한 직후 LangGraph `interrupt()` 로 멈추고 `status: "awaiting_plan_review"` 를 반환합니다. 웹 UI의 **✓ Proceed** 버튼 또는 `POST /api/workflow/tasks/{task_id}/plan/approve {"proceed": true, "comments": "..."}` 호출로 실행을 재개합니다.
  - `always_proceed`: 계획 작성 직후 중단 없이 코딩·검증·`walkthrough.md` 작성까지 자동 진행합니다.
- **이중 완료 게이트 (기본 ON)**: 결정론적 `PytestGateMiddleware`(`pytest` 실패 시 마지막 출력을 주입해 자가 수정 유도)와 `RubricMiddleware`(`AGENT_RUBRIC_ENABLED=true`)를 모두 통과한 경우에만 Git 커밋과 원격 브랜치 Push가 수행됩니다.

---

## 4-Act 라이브 데모 스토리라인

| 막 (Act) | 시연 시나리오 | 1줄 데모 프롬프트 | 실행 엔드포인트 / UI 위치 |
| :--- | :--- | :--- | :--- |
| **Act 1** | 낯선 프로젝트 구조 파악 & 소크라테스 온보딩 (오프라인 가능) | `"이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘"` | `POST /api/repo-onboard/analyze` · Tab 4 & Tab 5 |
| **Act 2 (기본)** | 격리 Worktree 웹앱 자율 빌드 + Plan Proceed + Live Preview (`project_map_app`) | `"이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"` | `POST /api/workflow/tasks/build` → `/plan/approve` → `/preview` · Tab 2 |
| **Act 2 (보조)** | 서비스 상태 점검 대시보드 웹앱 빌드 + Live Preview (`inspection_dashboard`) | `"서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘"` | `POST /api/workflow/tasks/build` (`preset="inspection_dashboard"`) · Tab 2 |
| **Act 3** | 위험 명령(`cd 'app' && FOO=1 git push`) & 런타임 비밀 파일 탈출(`../../.env`) 결정론적 차단 + 자연어 에이전트(`direct` / `repl`) | `"에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘"` | `POST /api/code-mode/run` (오프라인) & `POST /api/code-mode/agent` · Tab 6 |
| **Act 4** | 독립 읽기 전용 Reviewer(`ReviewFindings` + `EvaluatorRubric`)의 `file:line` 결함 지적(`REQUEST_CHANGES`) & 브랜치 승인(`APPROVE`) | `"방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘"` | `POST /api/workflow/tasks/review` · 좌측 Mode 2 & Tab 3 |

---

## 빠른 실행 및 검증 방법 (WSL2 / Linux 권장)

```bash
# 1) 환경변수 준비 (.env에 GEMINI_API_KEY 등 실제 키 입력)
cp .env.example .env

# 2) 오프라인 전체 단위·AST 아키텍처·보안·Scripted DeepAgent 그래프 테스트 실행
GEMINI_ENABLED=false QWEN_ENABLED=false OPENAI_ENABLED=false .venv/bin/pytest -q

# 3) 실제 Gemini API 기반 자가 수정 & 3단계 산출물 검증 (키 설정 시 실행, 미설정 시 자동 skip)
.venv/bin/pytest -m live_llm -v

# 4) FastAPI 웹 관제 서버 기동
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# 5) 원커맨드 4-Act 라이브 엔드투엔드 검증 (Plan 승인 포함)
bash scripts/demo.sh
```

---

## 사내 Pod & Private Self-Hosted GitLab 배포 보안

1. **Zero-Leak Git 인증 보안**: `app/core/git_source.py` 는 Git 2.31+ `GIT_CONFIG_COUNT` 환경변수로 호스트 범위(`http.<origin>/.extraHeader`) 인증 헤더만 주입하며 `sanitize_git_text()` 와 `GuardedWorktreeBackend` 가 모든 출력에서 토큰·API 키를 자동 스크러빙합니다.
2. **비루트 컨테이너 빌드 & 기동 (`Dockerfile`)**:
   ```bash
   docker build -t coding-agent-control-plane:latest .
   docker run --rm -p 8000:8000 --env-file .env coding-agent-control-plane:latest
   ```
3. **Kubernetes Liveness / Readiness Probes**: `GET /healthz` 및 `GET /readyz` 제공.
4. **폐쇄망(Air-Gap) UI 안전성**: `app/static/` 내 모든 CSS/JS 및 Live Preview 샌드박스(`<iframe sandbox="allow-scripts">`)는 외부 CDN·Google Fonts 요청이 **0건** 입니다.

상세 아키텍처 근거와 라이브 발표 진행 가이드는 [`docs/PRESENTATION_GUIDE.md`](docs/PRESENTATION_GUIDE.md) 를 참고하세요.
