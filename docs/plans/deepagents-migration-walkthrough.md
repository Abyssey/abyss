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
