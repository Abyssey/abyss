# Modern Coding Agent Architecture 발표 & 라이브 데모 가이드

본 문서는 업계 최신 코딩 에이전트 아키텍처의 공통 수렴 원리와 **Modern Python DDD Modular Monolith (`app/`)** 설계 근거, 그리고 청중(시니어 엔지니어 + 입문자) 모두에게 명확하게 전달되는 **4-Act 라이브 시연 스크립트** 를 정리한 기술 가이드입니다.

---

## 1. 아키텍처 설계 관점: 왜 이 구조인가?

### 1.1 업계 최신 코딩 에이전트의 공통 수렴 패턴
프로덕션급 코딩 에이전트 — **LangChain `open-swe`**, **Stripe Minions**, **Coinbase Cloudbot**, **Claude Code** — 는 브랜드가 달라도 다음 4가지 설계 원칙으로 수렴하고 있습니다:

1. **격리된 실행 환경 (Isolated Git Worktree / Sandbox)**: 에이전트가 `main` 브랜치나 개발자의 로컬 작업 폴더를 직접 건드리지 않고, 태스크별 독립 브랜치(`agent/task-*`)와 Worktree에서 개발·테스트합니다.
2. **결과를 눈으로 검증하는 시각적 빌드 & 라이브 프리뷰**: `open-swe` 의 `openwiki` 와 `docs/PLUGINS.md` 프리뷰 UX처럼, 에이전트가 실제 코드를 실행해 산출물을 만들고 안전한 샌드박스(`<iframe sandbox="allow-scripts">`, `allow-same-origin` 미부여) 안에서 즉시 결과를 클릭해 검증합니다.
3. **작성자와 분리된 독립 읽기 전용 리뷰어**: `open-swe` Reviewer 및 `walkthrough` 가이드 diff 리뷰처럼, 코드를 작성한 Builder와 분리된 읽기 전용 Reviewer가 `file:line` 단위로 보안 취약점·예외 처리 누락·테스트 부재를 지적하고 `REQUEST_CHANGES` / `APPROVE` 를 결정합니다.
4. **결정론적 가드레일 (Deterministic Guardrails)**: LLM 프롬프트의 선의에 의존하지 않고, 도구 실행 직전에 코드 레벨 정책으로 위험 명령과 경로 탈출을 차단합니다.

### 1.2 본 프로젝트의 핵심 차별점 (Claude Code Kick — 2대 Focus Domains)
공통 기반 위에 본 프로젝트는 **2개의 순수 표준 라이브러리 핵심 도메인** 을 가장 강조합니다:

- **★ Focus Domain #1 — `app/domains/guardrails/` (외부 의존성 0개)**:
  - [`app/domains/guardrails/isolation_policy.py`](../app/domains/guardrails/isolation_policy.py): ① 파일 수정 경로 검증(`Path.resolve().is_relative_to`, 심볼릭 링크·`../` 차단) ② 실행 CWD 검증 ③ Git Redirect(`git -C`, `--git-dir`, `cd ..`) 차단 ④ 동적 셸 구문(`${!var}`, `eval`, 백틱) 차단의 4-Check 결정론적 게이트.
  - [`app/domains/guardrails/permissions.py`](../app/domains/guardrails/permissions.py): `cd 'app' && FOO=1 git push origin main` 같은 복합 셸 명령을 따옴표·리다이렉션(`2>&1`)을 보존하며 세그먼트로 분리하고 환경변수 접두어를 정규화한 뒤 `deny > ask > allow` 규칙으로 차단.
- **★ Focus Domain #2 — `app/domains/execution/` (Claude Code Kick · Code Mode `REPL`)**:
  - [`app/domains/execution/code_mode.py`](../app/domains/execution/code_mode.py): 도구 1개마다 LLM을 왕복 호출하는 대신, 모델이 작성한 단일 비동기 `REPL` 코드 블록에서 검색·수정·병렬 읽기(`gather`)·테스트를 한 스텝에 수행하고 **마지막 표현식만** 모델에 반환합니다. STEP 0 정적 AST 검토와 STEP 2 내부 호출별 실시간 권한·격리·`PreToolUse` 훅(`onFailure: "block" | "continue"`)을 결합해 사내 **Qwen 3.8 Flash** 게이트웨이에서도 동일하게 실행됩니다.
  - [`app/domains/execution/runtime.py`](../app/domains/execution/runtime.py): Direct Tool Calling과 Code Mode `REPL` 듀얼 전환, `<think>` 추론 블록 실시간 분리, 그리고 **Auto-Compaction 기본 전략 (`context_strategy="compaction"`) + 선택적 Context Reset 인계 (`context_strategy="reset_handoff"`)** 를 지원합니다.

### 1.3 컨텍스트 관리: Auto-Compaction vs Context Reset (사실 기반 구분)
- **Claude Code CLI 기본 동작**: 긴 세션에서는 **Auto-Compaction (`/compact`, `CLAUDE.md`)** 이 기본으로 작동해 기존 대화를 압축하면서 작업을 이어갑니다.
- **선택적 Context Reset (`/clear` + `HandoffArtifact`)**: 작업 규모가 커지거나 Builder → Independent Reviewer 처럼 역할·관점을 전환할 때 구조화된 아티팩트만 넘기고 컨텍스트를 초기화하는 **보조 전략** 으로 활용합니다.

### 1.4 왜 Modern Python DDD Modular Monolith (`app/domains/*`) 인가?
과거의 수평적 4계층 분리는 기능 하나를 추적하려면 여러 최상위 디렉토리를 왕복해야 합니다. 본 프로젝트는 **Bounded Context 중심 수직 슬라이스** (`guardrails`, `execution`, `workflow`, `onboarding`) + **Shared Kernel (`core/`)** 로 구성하고, [`tests/test_ddd_architecture.py`](../tests/test_ddd_architecture.py) 의 **AST 정적 의존성 테스트 5종** 으로 도메인 경계 위반을 빌드 시점에 차단합니다.

---

## 2. 4-Act 라이브 발표 진행 스크립트 (약 10분)

발표 전 준비:
```bash
# 서버 기동 확인 (로컬 또는 Pod)
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/readyz
```
브라우저에서 `http://localhost:8000` 을 열고 상단 **4-Act Story Ribbon** 을 따라 진행합니다.

### Act 1 · 낯선 프로젝트 구조 파악 & 온보딩 (2분)
- **1줄 프롬프트**: `"이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘"`
- **시연 동작**:
  1. **Tab 4 (`Act 1 · 레거시 구조 파악 & 온보딩`)** 에서 `내 로컬 개발 폴더 (self)` (또는 실제 로컬 폴더 경로 `/path/to/my-project` / 사내 GitLab URL)로 분석 실행.
  2. 핵심 파일 읽기 순서, 레이어 요약, 3단계 소크라테스 문답을 확인하고, **Tab 5 (`Archify Architecture Map`)** 로 이동해 **`★ Focus Domain #1 · Guardrails`** 와 **`★ Focus Domain #2 · Execution`** 이 골드 테두리로 강조된 의존성 지도를 클릭해 설명합니다.
- **핵심 메시지**: *"Git 저장소가 아닌 일반 로컬 폴더도, 사내 GitLab 저장소도 토큰 유출 없이 AST로 구조를 즉시 파악합니다."*

### Act 2 · 격리 Worktree 웹앱 빌드 + 라이브 프리뷰 & Code Mode Kick (3분)
- **1줄 프롬프트 (기본 `project_map_app`)**: `"이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"`
- *(보조 프리셋 `inspection_dashboard`)*: `"서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘"`
- **시연 동작**:
  1. 좌측 **Mode 1 (`Act 2 · 만들기`)** 에서 기본 프리셋 **★ 프로젝트 지도 웹앱** 확인 후 **"▶ 격리 Worktree에서 만들기"** 클릭.
  2. 자동으로 열린 **Tab 2** 에서 `agent/task-*` 브랜치 생성, 실제 `git diff`, `pytest PASS`, 원격 Push 배지를 확인하고, 우측 **`<iframe sandbox="allow-scripts">` 라이브 프리뷰** 안에서 도메인 필터 칩·검색·파일 상세 인스펙터·입문자 체크리스트를 직접 클릭해 보여줍니다. 필요 시 **"새 탭으로 열기"** 를 클릭해 전체 화면으로도 보여줍니다.
  3. 이어서 **Tab 6 (`Code Mode REPL & 보안 가드레일 Lab`)** 에서 시나리오 1(`retry_policy`)을 실행해 **Direct 4턴 → Code Mode 1턴** 압축과 `Ctrl+O` Transcript Code 뷰를 보여줍니다.
- **핵심 메시지**: *"에이전트가 더미 텍스트가 아니라 실제 AST 스캐너 Python 코드와 테스트를 격리 브랜치에 작성·실행해, 클릭 가능한 웹앱을 즉석에서 띄워줍니다. 원본 폴더와 `main` 브랜치는 단 한 줄도 건드리지 않습니다."*

### Act 3 · 결정론적 보안 가드레일 차단 시연 (2분)
- **1줄 프롬프트**: `"에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘"`
- **시연 동작**:
  1. **Tab 6** 에서 시나리오 3(`cd 'app' && FOO=1 git push origin main`) 클릭 → `split_compound_command()` 가 두 번째 세그먼트를 파싱해 `Bash(git push*)` 규칙으로 `status: "refused"` (`permission:deny`) 차단 확인.
  2. 시나리오 4(`parts = ["..", "..", ".env"]; await write("/".join(parts), "LEAKED=1")`) 클릭 → 정적 AST 검토는 통과하더라도 STEP 2 실행 직전 `WorktreeIsolationPolicy` 가 실제 해석된 경로를 잡아내 `status: "refused"` (`isolation:file_edit`) 차단 확인.
  3. **Tab 1 (`Core Architecture & Focus Domains`)** 우측 코드 뷰어에서 [`app/domains/guardrails/isolation_policy.py`](../app/domains/guardrails/isolation_policy.py) 와 [`app/domains/guardrails/permissions.py`](../app/domains/guardrails/permissions.py) 실소스를 바로 보여줍니다.
- **핵심 메시지**: *"말로 부탁하는 프롬프트 가드레일이 아니라, 순수 표준 라이브러리 코드로 복합 셸 우회와 런타임 경로 탈출을 100% 결정론적으로 막습니다."*

### Act 4 · 독립 읽기 전용 코드 리뷰어 (`open-swe` Walkthrough) (2분)
- **1줄 프롬프트**: `"방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘"`
- **시연 동작**:
  1. **Tab 3 (`Act 4 · MR Code Review`)** 에서 **4-A: 결함 코드 리뷰** 클릭 → `REQUEST_CHANGES` 판정과 함께 `inspection_dashboard/api_handler.py:5` 하드코딩 API 키, 빈 `except: pass`, 테스트 파일 누락이 `file:line` 원인·수정 제안 카드로 출력되는 것을 확인합니다.
  2. 이어서 **4-B: Act 2 브랜치 리뷰** (또는 Tab 2의 **"→ Act 4: 방금 만든 브랜치 코드 리뷰하기"**) 클릭 → 방금 Act 2에서 만든 `agent/task-*` 브랜치를 **읽기 전용(`read_only_verified: true`)** 으로 검증하고 `pytest` 통과 확인 후 **`APPROVE`** 를 받는 것으로 발표를 마무리합니다.
- **핵심 메시지**: *"만드는 에이전트와 검증하는 에이전트를 분리해야 프로덕션에 안전하게 도입할 수 있습니다."*

---

## 3. Track A 터미널 자동 검증 & 사내 Pod 배포 체크리스트

### 터미널 전체 검증 (CI / 발표 직전 리허설)
```bash
# 1) 전체 단위·AST 아키텍처·보안·E2E 테스트
.venv/bin/pytest -v

# 2) Track A 4-Act 라이브 엔드투엔드 검증 (서버 기동 상태에서 실행)
bash scripts/demo.sh
```

### 사내 Pod + Private GCP VM Self-Hosted GitLab 체크리스트
- **컨테이너 이미지**: 비루트 `appuser` (UID 10001), `git` + `ca-certificates` 포함 (`Dockerfile`).
- **헬스 체크**: Kubernetes `livenessProbe` → `GET /healthz`, `readinessProbe` → `GET /readyz`.
- **사내 GitLab 인증**: `INTERNAL_GITLAB_URL`, `INTERNAL_GITLAB_TOKEN`, `INTERNAL_GIT_AUTH_MODE=basic_oauth2|private_token`, `INTERNAL_GIT_CA_BUNDLE` 설정 시 `GIT_CONFIG_COUNT` 환경변수로만 인증 헤더가 전달되며 URL·`.git/config`·에러 로그에 토큰이 절대 노출되지 않습니다.
- **폐쇄망 UI**: `app/static/` 에 Meta Astryx CSS가 로컬 번들되어 있으며 Google Fonts·외부 CDN 의존성이 0개입니다.
