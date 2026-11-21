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
