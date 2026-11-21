# deepagents 마이그레이션 Walkthrough

[계획 문서](deepagents-migration.md)의 Phase를 끝낼 때마다 아래에 기록을 하나씩 추가합니다(Antigravity의 walkthrough 형식). 각 기록은 **Changes Made / Validation Results / Notes** 세 부분으로 씁니다. Validation Results에는 실제로 실행한 명령과 결과를 그대로 붙입니다.

---

## Phase 0 — 계획 보관

### Changes Made
- [NEW] [deepagents-migration.md](deepagents-migration.md): 재시작 실행 계획(v2), 진행 체크리스트, 재개 방법, 개발 환경(Windows/WSL2) 안내.
- 결정 #8 추가: 엔진은 deepagents를 유지하고, Antigravity 코딩 에이전트의 작업 형식(implementation plan → task 체크리스트 → walkthrough)을 차용. `ArtifactWorkflowMiddleware`, Plan Review Policy(`AGENT_PLAN_REVIEW`), SDK 개념 대응표를 계획에 반영(Phase 3·5·6·7·8).
- [NEW] 이 walkthrough 문서.

### Validation Results
- 로컬 환경(Windows, Python 3.13 venv): `deepagents 0.7.41`, `langchain 1.5.2`, `langchain-core 1.8.4`, `langchain-openai 1.8.5`, `langchain-google-genai 4.5.2`, `langgraph 1.2.24` 설치 확인(검증된 조합과 일치).
- 기준선 `pytest -v` (Windows 네이티브, `main@99e9ee7` 코드 그대로): **54 passed, 12 failed (6분 19초)**. Linux 기준선 66 passed와 차이 나는 12건은 모두 OS 차이입니다.

| 원인 | 실패한 테스트 |
|:-|:-|
| POSIX `sh` 전제 (Code Mode 셸 실행) | `test_code_mode.py`의 doc_example, parallel_gather, agent_runtime_with_code_mode, scrubs_secret_env_vars, blocks_match_class… (5건) |
| 경로 구분자 `\` vs `/` | 위 parallel_gather 링크 감사, `test_isolation_policy.py` check_3 두 건(`git -C C:\…` 미검출) |
| 파일명에 `<>` 불가 | `test_coding_agent_tasks.py::test_preview_sets_csp…xss_filenames` |
| 심볼릭 링크 생성 권한(WinError 1314) | `test_isolation_policy.py::test_check_1_file_edit_blocks_main_checkout_symlink…` |
| 임시 clone 정리 실패(파일 잠금) | `test_internal_git.py::test_real_bare_git_clone_push_and_temp_cleanup` |
| 위 원인의 연쇄 | `test_orchestrator_e2e.py::test_multi_agent…`, `test_web_server.py::test_web_server_endpoints` |

### Notes
- 이 PC에는 Python 3.12가 없어 3.13으로 venv를 만들었습니다(`requires-python >=3.11`).
- WSL이 설치되어 있지 않습니다. 계획대로 Phase마다 "전체 green" 게이트를 지키려면 WSL2(Ubuntu) 설치 후 그 안에서 진행하는 것을 권장합니다. Windows 네이티브로 계속한다면 **게이트를 "기준선 12건 외 신규 실패 0건"으로 해석**합니다.

---

## Phase 1 — 의존성 & LangChain 모델 팩토리

### Changes Made
- [MODIFY] [pyproject.toml](../../pyproject.toml): `deepagents>=0.7.39,<0.8`, `langchain-openai>=1.0`, `langchain-google-genai>=4.4.3`를 기본 의존성으로 옮기고 `[project.optional-dependencies].deepagents`를 삭제.
- [MODIFY] [provider_rotator.py](../../app/core/llm/provider_rotator.py)
  - `LLMConfigurationError`, `LLM_CONFIG_REQUIRED_BANNER = "⛔ LLM CONFIG REQUIRED"` 추가.
  - `build_chat_model(agent_id, role) -> ChatModelSelection` 추가. `select_candidate_chain` 순서를 그대로 쓰고 mock·비활성·자격증명 없는 프로바이더는 건너뛰며, 건너뛴 이유를 `rotation_history`에 남김.
  - Gemini → `ChatGoogleGenerativeAI(timeout=90, max_retries=2)`, Qwen → `ChatOpenAI` + `httpx.Client(trust_env=False)`, OpenAI → `ChatOpenAI`.
  - mock 프로바이더와 `invoke_chat`은 그대로 둠(삭제는 Phase 6).
- [MODIFY] [main.py](../../app/main.py): `LLMConfigurationError` → HTTP 503 `{"status": "llm_config_required", "error": "⛔ LLM CONFIG REQUIRED", ...}` 예외 핸들러.
- [MODIFY] [.env.example](../../.env.example): `GEMINI_MODEL=gemini-3.8-flash`(로컬에서 바꿔 둔 값 반영), 선택 항목 `GEMINI_FALLBACK_MODEL` 안내 주석.
- [NEW] [test_llm_model_factory.py](../../tests/test_llm_model_factory.py): 6개 테스트(자격증명 없음 → 에러, Gemini 네이티브 모델, Flash 폴백 미들웨어, Qwen `EMPTY` 키 + 역할 체인, 주 프로바이더 건너뜀 기록, 503 매핑). 모든 테스트가 프로바이더를 명시적으로 설정해 로컬 `.env`의 실제 키에 영향받지 않음.

### 계획과 달라진 점
- 원안은 `tuple[BaseChatModel, str]` + `.with_fallbacks()`였지만, `RunnableWithFallbacks`에는 `bind_tools`가 없어 deepagents 그래프에 넣을 수 없습니다(로컬에서 확인). 그래서 `ChatModelSelection.middleware()`가 LangChain `ModelFallbackMiddleware`를 돌려주는 방식으로 바꿨습니다. Phase 3의 `DeepAgentRuntime`이 이 미들웨어를 `create_deep_agent(middleware=...)`에 넣습니다.
- Gemini Flash 폴백은 모델 이름을 하드코딩하지 않고 `GEMINI_FALLBACK_MODEL`을 설정했을 때만 겁니다.
- `trust_env=False`는 사내망 Qwen에만 적용합니다. 공용 OpenAI 엔드포인트는 회사 프록시가 필요할 수 있어서입니다.

### Validation Results
```text
> .venv\Scripts\pytest -q tests/test_llm_model_factory.py tests/test_provider_rotator.py tests/test_ddd_architecture.py tests/test_web_server.py::test_dockerfile_and_pyproject_pod_packaging_hygiene
21 passed in 48.00s

> .venv\Scripts\pytest -q -rf        (Windows 네이티브 전체)
12 failed, 60 passed in 390.52s
```
- 실패 12건은 [Phase 0 기준선](#phase-0--계획-보관)과 정확히 같은 목록입니다(신규 실패 0건). 통과는 54 → 60(+6, 새 테스트).

### Notes
- 계획 검토 방식은 사용자 결정에 따라 기본값을 `request_review`로 바꿨습니다(계획 문서의 Plan Review Policy 절과 Phase 5·6·7 반영).
- 기존 `tests/test_provider_rotator.py`는 mock 폴백을 전제로 하는데, `.env`에 실제 `GEMINI_API_KEY`가 있으면 Gemini OpenAI 호환 엔드포인트를 실제로 호출할 수 있습니다. 이 테스트는 Phase 6에서 mock을 지울 때 함께 정리합니다.

---

## Phase 2 — `GuardedWorktreeBackend`

### Changes Made
- 개발 환경을 WSL2 Ubuntu 26.04로 옮김: 작업 사본 `/home/abyss/projects/coding_agent`, Python 3.12.16(사용자 영역 `uv`), 패키지 조합은 검증된 조합과 동일. Phase 1 상태에서 **72 passed**(Windows에서 실패하던 12건 포함 전부 통과) 확인.
- [NEW] [deepagent_backend.py](../../app/domains/execution/deepagent_backend.py)
  - `GuardedWorktreeBackend(LocalShellBackend)`: `ls`/`read`/`glob`/`grep`/`download` → Check #1b(읽기 범위), `write`/`edit`/`delete`/`upload` → Check #1(파일 수정), 그 뒤 `PermissionRules`(`.env*`는 깊이와 상관없이 거부). `execute` → `PermissionRules.evaluate("Bash")` → `validate_shell_command`(Check #2~#4) → 실행 → 시크릿 마스킹 → 앞뒤 보존 절단.
  - `ReadOnlyGuardedBackend(FilesystemBackend)`: 같은 읽기 가드 + 모든 쓰기 거부, `execute` 없음.
  - `BlockedAttempt` 기록 + `on_block` 콜백(감사 이벤트용), 피드백 형식 `[WORKTREE ISOLATION REFUSAL - <check>] … Hint: …`.
  - `_safe_env`: 시크릿 환경변수 제거, `worktree/.venv/bin` → 현재 인터프리터 bin 순으로 PATH 앞에 둠(Windows면 `Scripts`).
  - `DEFAULT_PERMISSIONS`(deepagents `FilesystemPermission`): Phase 3에서 `create_deep_agent(permissions=...)`에 넘김.
- [NEW] [test_deepagent_backend.py](../../tests/test_deepagent_backend.py): 21개 테스트.

### Validation Results
```text
$ .venv/bin/pytest -q tests/test_deepagent_backend.py tests/test_ddd_architecture.py
33 passed in 2.98s

$ .venv/bin/pytest -q            (WSL2 전체)
93 passed in 215.61s
```
검증한 시나리오: `../../main/.env` 탈출, 심볼릭 링크 탈출, `.git/config` 수정·읽기, `.env`/`config/.env.production`, 목록·검색에서 `.git`·`.env` 숨김, `read`/`grep`/`cat`/`env` 출력의 시크릿 마스킹, `cd app && git push origin main`·`git -C ../../main status`·`git reset --hard`·`sh -c`·`$(...)`·`cd ../../main`·`curl`·`pip install` 차단, worktree 안 `pytest` 통과/실패 감지, 긴 출력 앞뒤 보존, 읽기 전용 백엔드의 쓰기·실행 거부.

### Notes
- `PermissionRules`의 `ask` 규칙은 자율 실행 중에는 거부로 처리합니다. 사람 승인으로 이어 주는 방식은 Phase 3에서 `FilesystemPermission(mode="interrupt")`와 `interrupt_on`을 검토합니다.
- 차단 검사 순서상 `git -C ../../main reset --hard`는 Check #3보다 먼저 `permission:ask`(git reset --hard)로 걸립니다. 어느 쪽이든 실행되지 않습니다.
- Windows 쪽 작업 사본(`C:\Users\User\Desktop\Dev\abyss`)은 이제 보조용입니다. 이어서 작업할 때는 WSL 작업 사본을 쓰세요.

---

## Phase 3 — `DeepAgentRuntime` & Antigravity 3-Phase Artifact Workflow

### Changes Made
- [NEW] [artifact_workflow.py](../../app/domains/execution/artifact_workflow.py)
  - Antigravity 방식의 3단계 코딩 에이전트 흐름(`PLANNING → EXECUTION → VERIFICATION/WALKTHROUGH`)을 `ArtifactWorkflowMiddleware` + `PlanGateMiddleware`로 구현.
  - `CompositeBackend`로 `/artifacts/` 경로를 Git Worktree 밖(`data/tasks/<id>/artifacts/` 또는 임시 디렉터리)으로 분리 라우팅해 `git status`와 사용자 커밋을 오염시키지 않음.
  - PLANNING 단계에서는 읽기 전용 탐색만 허용하고 워크스페이스 수정·셸 실행은 `[PLAN-FIRST REFUSAL]`로 거부.
  - `AGENT_PLAN_REVIEW` 정책(`request_review` 기본값 / `always_proceed`): `request_review`일 때 `/artifacts/implementation_plan.md` 작성 직후 LangGraph `interrupt()`로 일시 정지하며, `proceed=False`(계획 수정 요청)와 `proceed=True` + 인라인 코멘트 주입 재개를 모두 지원.
  - `write_todos` 호출 결과를 `/artifacts/task.md`(`[ ]` / `[/]` / `[x]`)로 결정론적 렌더링.
  - 게이트 통과 후 `/artifacts/walkthrough.md` 필수 섹션(`Changes Made`, `Validation Results`) 누락 시 재요구(최대 2회)하고, 실제 `pytest` 게이트 실행 출력을 `Validation Results` 하단에 결정론적으로 스탬핑(할루시네이션 방지).
- [MODIFY] [runtime.py](../../app/domains/execution/runtime.py)
  - `DeepAgentRuntime`, `DeepAgentResult`, `PytestGateMiddleware`, `_GatedRubricMiddleware`, `TurnTraceEntry`, `SubagentCallRecord`, `PytestGateRun` 추가(구 `AgentRuntime`은 Phase 6까지 병행 유지).
  - `create_deep_agent` 그래프 조립: `TodoListMiddleware`, `ArtifactWorkflowMiddleware`, `PytestGateMiddleware`, `RubricMiddleware`(`AGENT_RUBRIC_ENABLED` 기본 ON), 읽기 전용 서브에이전트(`repo-explorer`, `code-reviewer`), `/AGENTS.md`·`/CLAUDE.md` 메모리 자동 로드, `mode="repl"`용 `run_python`(`CodeModeExecutor`) 도구 연결.
- [MODIFY] [deepagent_backend.py](../../app/domains/execution/deepagent_backend.py): `FilesystemBackend._real_path(key) -> str`와의 메서드 이름 충돌을 막기 위해 내부 헬퍼를 `_host_path`로 변경하고 프리플라이트 `guard_tool_path` 추가.
- [MODIFY] [test_ddd_architecture.py](../../tests/test_ddd_architecture.py): Rule #2를 갱신해 `code_mode.py`는 순수 stdlib + guardrails만 유지하고, `runtime.py`·`deepagent_backend.py`·`artifact_workflow.py`는 `deepagents`/`langchain*`/`langgraph`를 허용하되 `workflow`/`onboarding`/`app.core`/`fastapi` 역참조를 차단.
- [NEW] [helpers_llm_doubles.py](../../tests/helpers_llm_doubles.py), [test_deepagent_runtime.py](../../tests/test_deepagent_runtime.py), [test_artifact_workflow.py](../../tests/test_artifact_workflow.py): `ScriptedDeepAgentChatModel`(`tests/` 전용) 및 엔드투엔드 그래프 테스트 10개 추가.

### 계획과 달라진 점
- `deepagents 0.7.41`의 `FilesystemMiddleware`는 `SandboxBackendProtocol`(`execute` 지원 백엔드)과 `FilesystemPermission`을 동시에 넘기면 `NotImplementedError`를 발생시킵니다. 실행 백엔드(`GuardedWorktreeBackend`)에서는 백엔드 자체의 4-Check + `PermissionRules`와 `ArtifactWorkflowMiddleware` 프리플라이트 검사가 파일·셸 권한을 모두 강제하므로 `permissions=None`을 넘기고, 비실행 백엔드(`ReadOnlyGuardedBackend`)에만 `DEFAULT_PERMISSIONS`를 함께 전달합니다. 서브에이전트(`repo-explorer`, `code-reviewer`)의 읽기 전용 격리는 `_ReadOnlySubagentMiddleware`로 강제했습니다.
- LangChain `create_agent`의 `after_agent` 체인은 미들웨어 목록의 역순(안쪽 → 바깥쪽)으로 실행됩니다. 실행 순서가 `PlanGate → PytestGate → RubricMiddleware → WalkthroughGate`가 되도록 스택 순서를 배치했고, 워크스페이스 변경 없이 walkthrough만 작성한 턴에서는 `PytestGate`와 `RubricMiddleware`가 중복 재실행되지 않도록 변경 시퀀스 추적기를 두었습니다.

### Validation Results
```text
$ .venv/bin/pytest -q tests/test_deepagent_runtime.py tests/test_artifact_workflow.py tests/test_deepagent_backend.py tests/test_ddd_architecture.py
43 passed in 10.05s

$ GEMINI_ENABLED=false QWEN_ENABLED=false OPENAI_ENABLED=false .venv/bin/pytest -q
103 passed in 147.79s (0:02:27)
```

---

## Phase 4 — Execution API (`POST /api/code-mode/agent`)

### Changes Made
- [MODIFY] [schemas.py](../../app/domains/execution/schemas.py): `CodeModeAgentRequest(instruction, mode: "direct" | "repl", scenario_id?, reset_sandbox?, completion_cmd?, rubric?)` 요청 스키마 추가(빈 지시문 HTTP 422 검증 포함).
- [MODIFY] [service.py](../../app/domains/execution/service.py): `CodeModeLabService`에 `model_factory` 콜백 주입과 `run_agent` 메서드 추가. `app.core`를 직접 import하지 않고(DDD 경계 유지) `GuardedWorktreeBackend` + `DeepAgentRuntime(mode="direct" | "repl", workflow="fast")`으로 자연어 지시문을 샌드박스에서 실행. 기존 `POST /api/code-mode/run` 오프라인 가드레일 시나리오는 그대로 유지.
- [MODIFY] [router.py](../../app/domains/execution/router.py): `POST /api/code-mode/agent` 엔드포인트 등록.
- [MODIFY] [container.py](../../app/core/container.py): `CodeModeLabService(..., model_factory=rotator.build_chat_model)` 연결(자격증명 미설정 시 `LLMConfigurationError` → HTTP 503 `⛔ LLM CONFIG REQUIRED`).
- [NEW] [test_code_mode_agent_api.py](../../tests/test_code_mode_agent_api.py): `repl` 모드, `direct` 모드 + 가드레일 차단 기록, 자격증명 미설정 시 HTTP 503, 오프라인 `/run` 시나리오 동작 검증 테스트 4개 추가.

### Validation Results
```text
$ .venv/bin/pytest -q tests/test_code_mode_agent_api.py tests/test_code_mode.py tests/test_ddd_architecture.py
32 passed in 6.91s

$ GEMINI_ENABLED=false QWEN_ENABLED=false OPENAI_ENABLED=false .venv/bin/pytest -q
107 passed in 151.63s (0:02:31)
```
