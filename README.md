# Modern Coding Agent Architecture — Guardrails, Code Mode (`REPL`) & Isolated Git Worktrees

최신 코딩 에이전트 아키텍처(LangChain [`open-swe`](https://github.com/langchain-ai/open-swe), Stripe Minions, Coinbase Cloudbot, Claude Code)가 공통적으로 수렴한 핵심 설계 패턴을 **Modern Python DDD Modular Monolith + FastAPI (`app/`)** 로 구현한 실전형 코딩 에이전트 하네스 및 웹 관제 플레인입니다.

- **업계 공통 아키텍처 기반 (`open-swe` · Minions · Cloudbot · Claude Code)**: 격리된 Git Worktree 개발 환경, 독립 읽기 전용 MR/브랜치 코드 리뷰(`open-swe` Reviewer + `walkthrough`), AST 기반 프로젝트 지도 웹앱 생성 및 샌드박스 라이브 프리뷰(`open-swe` `openwiki` + `docs/PLUGINS.md` 프리뷰 패턴)를 제공합니다.
- **Claude Code 핵심 차별점 (Kick — 2대 Focus Domains)**:
  1. **★ Focus Domain #1 (`app/domains/guardrails/`)**: 외부 의존성이 0개인 순수 Python 표준 라이브러리로 **4-Check Git Worktree 격리**와 **복합 셸 명령 세그먼트 권한 게이트(`deny > ask > allow`)** 를 결정론적으로 강제합니다.
  2. **★ Focus Domain #2 (`app/domains/execution/`)**: 도구 1개씩 왕복하는 대신 단일 `REPL` 실행 1회로 검색·수정·병렬 읽기(`gather`)·테스트를 수행하고 최종 결과만 반환하는 **Code Mode (`REPL`)** 를 구현해, 사내 OpenAI 호환 게이트웨이(**Qwen 3.8 Flash**), **Gemini**, **OpenAI**, 오프라인 **Deterministic Simulator** 어디서든 구동합니다.
- **로컬 폴더 우선 테스트 + 사내 Pod / Self-Hosted GitLab 배포 대응**: Git 초기화 전 일반 로컬 폴더(`/path/to/my-project`, `self`)를 원본 파일 손상 없이 바로 분석·빌드할 수 있고, 사내 폐쇄망 Pod에서 Private GCP VM의 **Self-Hosted GitLab** 에 토큰 무노출(`GIT_CONFIG_COUNT` 호스트 범위 인증 헤더)로 안전하게 연결합니다.

---

## Modern Python DDD Modular Monolith 디렉토리 구조 (`app/`)

수평적 4계층 폴더 분리 대신 **비즈니스 도메인(Bounded Context) 중심의 수직 슬라이스** 로 구성해, 시니어 엔지니어가 핵심 도메인(`guardrails`, `execution`)과 워크플로우·온보딩·공유 커널의 경계를 즉시 검증할 수 있습니다.

```text
app/
├── main.py                                   # Composition Root: create_app(), lifespan, /healthz, /readyz
├── core/                                     # Shared Kernel (공통 설정·보안 Git 리졸버·DI·멀티 LLM 라우터)
│   ├── config.py                             #   AppSettings (로컬 / Pod / 사내 GitLab / TLS 설정)
│   ├── git_source.py                         #   InternalGitSourceResolver (GIT_CONFIG_COUNT 무노출 인증 · 마스킹)
│   ├── container.py                          #   AppContainer & FastAPI Depends() 프로바이더
│   └── llm/
│       ├── provider_rotator.py               #   Qwen 3.8 Flash / Gemini / OpenAI / Mock 멀티 LLM 로테이터
│       └── router.py                         #   /api/providers/*
├── domains/                                  # Bounded Contexts (도메인별 수직 슬라이스)
│   ├── guardrails/                           # ★ Focus Domain #1 (Pure Python Stdlib · 외부 의존성 0개 · router 없음)
│   │   ├── isolation_policy.py               #   4-Check Worktree 경로·CWD·Git Redirect·동적 셸 검증
│   │   └── permissions.py                    #   복합 셸 명령 세그먼트 분리기 & deny > ask > allow 엔진
│   ├── execution/                            # ★ Focus Domain #2 (Claude Code Kick · Pure Stdlib + guardrails만 참조)
│   │   ├── code_mode.py                      #   Code Mode (REPL) AST 정적 검토 + 호출별 권한/격리/Hook 게이트
│   │   ├── runtime.py                        #   Direct/REPL 듀얼 루프 (<think> 분리 · Compaction / Reset Handoff)
│   │   ├── service.py                        #   Code Mode & 보안 가드레일 실습 샌드박스 서비스
│   │   ├── schemas.py                        #   Pydantic DTO
│   │   └── router.py                         #   /api/code-mode/*
│   ├── workflow/                             # 빌드·프리뷰·리뷰·멀티 에이전트 팀 조율 도메인
│   │   ├── evaluator.py                      #   Pure Stdlib 독립 품질 게이트(100점) & Walkthrough Diff 리뷰어
│   │   ├── task_service.py                   #   Act 2 시각 웹앱 빌드 + Live Preview + Act 4 읽기 전용 리뷰 서비스
│   │   ├── coordinator.py                    #   3-Worktree 병렬 팀 오케스트레이터 (Task Board · Mailbox · Merge)
│   │   ├── worktree_adapter.py               #   Git Worktree 생성·Lock·Merge 및 Safe Push 어댑터
│   │   ├── worktree_tools.py                 #   Worktree 범위 파일/셸/메일박스 도구 클로저
│   │   ├── catalog_service.py                #   핵심 도메인 실소스 카탈로그 서비스 (/api/learning-core)
│   │   ├── deepagents_adapter.py             #   LangChain DeepAgents SDK 브리지 어댑터
│   │   ├── seed_templates.py                 #   3-Worktree 데모 워크스페이스 코드/테스트 템플릿
│   │   ├── schemas.py                        #   Build / Review / Workflow Pydantic DTO
│   │   └── router.py                         #   /, /api/state, /api/workflow/*, /api/events/stream, /api/learning-core
│   └── onboarding/                           # 독립 온보딩 도메인 (다른 domains/* import 0개)
│       ├── git_source.py                     #   Core Git 리졸버 연동
│       ├── templates.py                      #   AST 저장소 분석기 (Archify IR) & 3단계 소크라테스 튜터
│       ├── service.py                        #   로컬 폴더(Git/비-Git) & 사내 GitLab 분석 서비스
│       ├── schemas.py                        #   Pydantic DTO
│       └── router.py                         #   /api/repo-onboard/*
└── static/                                   # Air-Gap Meta Astryx 6-탭 웹 관제 UI (외부 CDN/폰트 0개)
```

### AST 기반 아키텍처 경계 검증 (`tests/test_ddd_architecture.py`)
1. `app/domains/guardrails/*.py`: **100% Python 표준 라이브러리 + 내부 패키지** 만 허용 (HTTP 라우터 없음).
2. `app/domains/execution/{code_mode,runtime}.py`: 표준 라이브러리 + `app.domains.guardrails.*` 만 허용.
3. `app/domains/workflow/evaluator.py`: **100% Python 표준 라이브러리** 만 허용.
4. `app/domains/onboarding/*.py`: `workflow`, `execution`, `guardrails`, `core.llm`, `app.main` 교차 import 금지 (완전 독립 도메인).
5. 모든 `router.py`: `fastapi`, `pydantic`, `app.core.container`, 자기 도메인 패키지 외 import 금지.

---

## 컨텍스트 수명주기 전략: Auto-Compaction 기본 + Optional Context Reset

- **Claude Code CLI의 기본 전략은 Auto-Compaction (`/compact`, `CLAUDE.md`)** 입니다. 일반 개발 대화에서는 자동 압축으로 작업 맥락을 유지하는 것이 기본 동작입니다.
- 반면 장시간 멀티-스텝 빌드나 Builder → Independent Reviewer 간 역할 전환 시에는 **Context Reset (`/clear`) + 구조화된 `HandoffArtifact`** 를 선택해 불필요한 노이즈 없이 검증된 산출물만 인계할 수 있습니다.
- [`app/domains/execution/runtime.py`](app/domains/execution/runtime.py) 는 `context_strategy="compaction"` (기본값)과 `context_strategy="reset_handoff"` (선택 인계 모드)를 모두 명시적으로 지원합니다.

---

## 4-Act 라이브 데모 스토리라인 (자연스러운 1줄 한국어 프롬프트 5종)

비개발자·입문자도 흐름을 즉시 이해할 수 있도록 **4막(4-Act) 스토리** 와 **1줄 프롬프트** 로 전 과정을 시연합니다:

| 막 (Act) | 시연 시나리오 | 1줄 데모 프롬프트 | 실행 엔드포인트 / UI 위치 |
| :--- | :--- | :--- | :--- |
| **Act 1** | 낯선 프로젝트 구조 파악 & 소크라테스 온보딩 | `"이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘"` | `POST /api/repo-onboard/analyze` · Tab 4 & Tab 5 |
| **Act 2 (기본)** | 격리 Worktree에서 인터랙티브 프로젝트 지도 웹앱 빌드 + Live Preview (`project_map_app`) | `"이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"` | `POST /api/workflow/tasks/build` + `GET /api/workflow/tasks/{task_id}/preview` · 좌측 Mode 1 & Tab 2 |
| **Act 2 (보조)** | 서비스 상태 점검 대시보드 웹앱 빌드 + Live Preview (`inspection_dashboard`) | `"서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘"` | `POST /api/workflow/tasks/build` (`preset="inspection_dashboard"`) · 좌측 Mode 1 & Tab 2 |
| **Act 3** | 위험 명령(`cd 'app' && FOO=1 git push`) & 런타임 비밀 파일 탈출(`../../.env`) 결정론적 차단 | `"에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘"` | `POST /api/code-mode/run` (`status: "refused"`) · Tab 6 |
| **Act 4** | 독립 읽기 전용 Reviewer의 `file:line` 결함 지적(`REQUEST_CHANGES`) & Act 2 브랜치 승인(`APPROVE`) | `"방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘"` | `POST /api/workflow/tasks/review` · 좌측 Mode 2 & Tab 3 |

---

## 빠른 실행 및 2-Track 데모 검증

### 1. 테스트 스위트 실행 및 서버 기동

```bash
# 1) 전체 단위·아키텍처·보안·E2E 테스트 실행
.venv/bin/pytest -v

# 2) FastAPI 웹 관제 서버 실행 (로컬 / Pod 동일)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Track A — 원커맨드 4-Act CLI 검증 (`scripts/demo.sh`)

```bash
# Pod 헬스 체크
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/readyz

# Act 1 ~ Act 4 전체 자동 검증 (Git Worktree 생성·테스트·Push·Live Preview HTML·가드레일 차단·MR 리뷰)
bash scripts/demo.sh
```

`scripts/demo.sh` 실행이 끝나면 터미널 마지막 줄에 생성된 **실제 인터랙티브 프로젝트 지도 웹페이지 주소** (`http://localhost:8000/api/workflow/tasks/<task_id>/preview`)가 출력됩니다.

### 3. Track B — 웹 관제 UI 시연 (`http://localhost:8000`)

1. 상단 **4-Act Story Ribbon** 에서 `Act 1 → Act 2 → Act 3 → Act 4` 순서대로 이동할 수 있습니다.
2. **Act 1 (Tab 4)**: 대상 저장소에 `내 로컬 개발 폴더 (self)`, 로컬 경로(`/path/to/my-project`), 사내 GitLab URL, 또는 `데모용 내장 샘플 저장소 (demo)` 를 입력해 구조 분석과 Tab 5 Archify 지도(★ Focus Domains 골드 강조)를 확인합니다.
3. **Act 2 (좌측 Mode 1 → Tab 2)**: 기본 프리셋 **★ 프로젝트 지도 웹앱** 으로 `"격리 Worktree에서 만들기"` 를 실행하면, Tab 2 우측에 **`<iframe sandbox="allow-scripts">` 라이브 프리뷰** 와 **"새 탭으로 열기"** 링크가 즉시 표시됩니다.
4. **Act 3 (Tab 6)**: 시나리오 1(`retry_policy`, 4턴 → 1턴)과 시나리오 3·4(복합 명령 `git push` 우회 차단 & 런타임 `../../.env` 탈출 차단, `status: "refused"`)를 실행하고 `Ctrl+O` 로 Transcript Code 뷰를 토글합니다.
5. **Act 4 (좌측 Mode 2 → Tab 3)**: **4-A: 결함 코드 리뷰** (`REQUEST_CHANGES`, `file:line` 원인·수정 제안)와 **4-B: Act 2 만든 브랜치 리뷰** (`APPROVE`, 읽기 전용 `pytest` 통과)를 실행합니다.

---

## 실제 배포 가이드: 로컬 폴더 우선 → 사내 Pod & Self-Hosted GitLab

### 모드 1 · 내 로컬 개발 폴더 첫 테스트 (설정 0개)
- 별도 `.env` 나 GitLab 설정 없이 바로 실행됩니다.
- `self`, `demo`, 또는 내 컴퓨터의 임의 폴더 경로(`/home/dev/my-project`)를 입력할 수 있으며, **Git 저장소가 아닌 일반 폴더도 지원** 합니다.
- Act 2 빌드 시 원본 로컬 폴더와 `main` 브랜치는 **절대 수정하지 않고**, 격리된 스냅샷/Worktree(`agent/task-*`) 안에서만 파일을 생성·테스트합니다.

### 모드 2 · 사내 Kubernetes/컨테이너 Pod + Private GCP VM Self-Hosted GitLab
1. `.env.example` 을 복사해 사내 GitLab 및 사내 LLM(`Qwen 3.8 Flash`) 환경변수를 설정합니다:
   ```bash
   cp .env.example .env
   # INTERNAL_GITLAB_URL=https://gitlab.internal.corp
   # INTERNAL_GITLAB_TOKEN=fake-token-xxxxxxxxxxxx
   # INTERNAL_GIT_AUTH_MODE=basic_oauth2       # 또는 private_token
   # INTERNAL_GIT_CA_BUNDLE=/etc/ssl/certs/corp-root-ca.pem
   # INTERNAL_GIT_SSL_VERIFY=true
   # QWEN_BASE_URL=http://<사내-vLLM-게이트웨이>/v1
   # QWEN_API_KEY=<사내-토큰>
   # QWEN_MODEL=qwen3.8-flash
   ```
2. **Zero-Leak Git 인증 보안**: `app/core/git_source.py` 는 Git 2.31+ `GIT_CONFIG_COUNT` 환경변수로 호스트 범위(`http.<origin>/.extraHeader`) 인증 헤더만 주입합니다. 토큰은 URL, 프로세스 인자(`-c`), `.git/config`, HTTP 응답, 에러 로그 어디에도 남지 않으며 `sanitize_git_text()` 로 자동 마스킹됩니다.
3. **비루트 컨테이너 빌드 & 기동 (`Dockerfile`)**:
   ```bash
   docker build -t coding-agent-control-plane:latest .
   docker run --rm -p 8000:8000 --env-file .env coding-agent-control-plane:latest
   ```
4. **Kubernetes Liveness / Readiness Probes**:
   - Liveness: `GET /healthz` (`{"status": "ok"}`)
   - Readiness: `GET /readyz` (`git` 바이너리, 정적 UI, 쓰기 가능 워크스페이스, 로컬 원격 픽스처 상태 검증)
5. **폐쇄망(Air-Gap) UI 안전성**: `app/static/` 내 모든 CSS/JS 및 생성되는 Live Preview HTML은 외부 CDN·Google Fonts 요청이 **0건** 입니다.

상세 아키텍처 근거와 라이브 발표 진행 가이드는 [`docs/PRESENTATION_GUIDE.md`](docs/PRESENTATION_GUIDE.md) 를 참고하세요.
