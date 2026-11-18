# Abyss — Claude Code Multi-Agent Worktree & Code Mode Architecture

**Abyss**는 공식 Claude Code 문서(`code.claude.com/docs`, v2.1.234 ~ v2.1.326+)의 최신 아키텍처 스펙인 **Experimental Code Mode (`CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1`, `REPL` 단일 도구 배치 실행)**, **Auto Mode 분류기 사각지대를 보완하는 복합 명령 세그먼트 권한 게이트 (`deny` / `ask` / `PreToolUse`)**, 그리고 **Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) + 4-Check Git Worktree 하이브리드 격리**를 파이썬으로 구현한 멀티 에이전트 코딩 시스템 및 웹 관제 플레인입니다.

공식 Claude Code 바이너리의 Code Mode는 커스텀 게이트웨이(`ANTHROPIC_BASE_URL`)나 비대화형 `-p`/SDK 환경에서 `"it only works directly with the Anthropic API"` 오류로 비활성화되지만, **Abyss**는 Code Mode 실행기와 3단계 보안 게이트를 파이썬으로 직접 구현하여 사내 LLM(**Qwen 3.8 Flash**), **Google Gemini**, **OpenAI**, 그리고 오프라인 **Deterministic Simulator** 어디서든 동일하게 구동할 수 있습니다.

---

## 핵심 아키텍처 및 모듈 구성

### 1. 5대 코어 모듈 (`abyss/core/`)
| 모듈 | 공식 Claude Code 스펙 (`code.claude.com/docs`) 매핑 및 구현 내용 |
| :--- | :--- |
| [`abyss/core/code_mode.py`](abyss/core/code_mode.py) | **Code Mode (`REPL` 도구 · v2.1.323+/v2.1.326+)**: 단일 `REPL` 스텝에서 병렬 실행(`gather`)·출력 필터링·조건부 수정을 수행하고 마지막 표현식만 반환. STEP 0 정적 AST 검토 + STEP 2 내부 도구 호출별 실시간 권한/격리/`PreToolUse` 훅(`the hook did not finish`, `onFailure: "block" \| "continue"`) 강제 + Foreground(10분)/Background(2시간) 타임아웃 및 `Ctrl+O` Transcript Code 뷰 분리 |
| [`abyss/core/permissions.py`](abyss/core/permissions.py) | **복합 셸 명령 세그먼트 분리 및 `deny` / `ask` 규칙 엔진**: Code Mode에서 `cd 'app' && FOO=1 git push` 형태로 전달되는 복합 명령을 따옴표·리다이렉션(`2>&1`) 보존 파서([`split_compound_command`](abyss/core/permissions.py))로 분리하여 세그먼트별 `Bash(git push*)`, `Edit(.env)` 규칙 매칭 |
| [`abyss/core/isolation_policy.py`](abyss/core/isolation_policy.py) | **4대 결정론적 Worktree 격리 게이트**: ① 파일 수정 경로 이탈 차단(`validate_file_edit`) ② 명령 실행 CWD 검증(`validate_command_cwd`) ③ `git -C` / `--git-dir` / `cd ..` 공유 `.git` 오염 차단(`validate_git_redirect`) ④ 동적 셸 치환 차단(`validate_command_shape`) |
| [`abyss/core/agent_runtime.py`](abyss/core/agent_runtime.py) | **Direct & Code Mode 겸용 에이전트 루프**: `<think>` 추론 블록 실시간 분리 추출, `code_mode` 활성화 시 단일 `REPL` 도구 스키마 자동 전환, 컨텍스트 리셋용 구조화된 [`HandoffArtifact`](abyss/core/agent_runtime.py) 생성 |
| [`abyss/core/evaluator_rubric.py`](abyss/core/evaluator_rubric.py) | **독립 품질 게이트 (`TaskCompleted` / Evaluator Rubric)**: Worker와 분리된 Reviewer가 `pytest` 결과(35점), 격리 준수(25점), `git diff` 스코프(20점), 메일박스 스키마 계약(20점)을 채점하여 80점 이상만 `main` 브랜치 머지 승인 |

### 2. 멀티 에이전트 오케스트레이터 & 웹 관제 인프라 (`abyss/orchestrator/`, `abyss/llm/`, `abyss/web/`)
- [`abyss/orchestrator/worktree_manager.py`](abyss/orchestrator/worktree_manager.py): `git worktree add/remove`, 브랜치별 `diff`, `merge --no-ff` 수명주기 관리
- [`abyss/orchestrator/team_coordinator.py`](abyss/orchestrator/team_coordinator.py): 파일 락 기반 Shared Task Board(`.abyss/tasks.json`), P2P Mailbox(`.jsonl`), 3개 병렬 Worktree Teammate 실행 및 `RepoOnboard` 서비스 자동 빌드·머지
- [`abyss/orchestrator/deepagents_bridge.py`](abyss/orchestrator/deepagents_bridge.py): `langchain-ai/deepagents` SDK 상태 점검 및 그래프 브릿지
- [`abyss/llm/provider_rotator.py`](abyss/llm/provider_rotator.py): `Qwen 3.8 Flash` / `Gemini` / `OpenAI` / `Mock` 프로바이더 로테이션 (`role_based`, `round_robin`, `fallback`, `single`)
- [`abyss/web/server.py`](abyss/web/server.py) & [`abyss/web/static/`](abyss/web/static/): **Meta Astryx**(`@astryxdesign/core`, `@astryxdesign/theme-neutral`) 디자인 시스템 기반 6-탭 실시간 관제 UI (`6. Code Mode (REPL · Ctrl+O) Lab` 포함)

---

## 빠른 실행 (Quickstart)

```bash
# 1. 전체 테스트 스위트 실행 (15 passed: Worktree 4대 격리, Code Mode & 복합 명령 권한 7종, LLM 로테이터, 3-Worktree E2E, Web API)
.venv/bin/pytest -v

# 2. 웹 관제 UI 및 RepoOnboard 서버 실행
.venv/bin/uvicorn abyss.web.server:app --host 0.0.0.0 --port 8000
```

아키텍처 변경점 상세 분석 및 데모 시연 가이드는 [`docs/PRESENTATION_GUIDE.md`](docs/PRESENTATION_GUIDE.md)를 참고하세요.
