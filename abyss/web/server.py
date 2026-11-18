"""
abyss/web/server.py
FastAPI Control Plane Server for Abyss Multi-Agent Coding Harness, Code Mode Lab, & RepoOnboard Demo Service.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from abyss.core.code_mode import CodeModeExecutor, HookSpec
from abyss.core.permissions import PermissionRules
from abyss.llm.provider_rotator import LLMProviderRotator
from abyss.orchestrator.deepagents_bridge import create_worktree_deep_agent
from abyss.orchestrator.team_coordinator import MultiAgentTeamCoordinator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO_WORKSPACE = REPO_ROOT / "demo_workspace" / "repo_onboard"
CODE_MODE_SANDBOX = DEMO_WORKSPACE / ".abyss" / "code_mode_sandbox"

rotator = LLMProviderRotator()
coordinator = MultiAgentTeamCoordinator(workspace_root=DEMO_WORKSPACE, rotator=rotator)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    # 서버 시작 시 데모 워크스페이스에 아직 머지된 파일이 없으면 1회 초기 빌드 수행
    if not (DEMO_WORKSPACE / "repo_onboard" / "service.py").exists():
        await coordinator.execute_full_workflow(
            "Build RepoOnboard: GitLab/GitHub Codebase Onboarding & Socratic Q&A Service"
        )
    _reset_code_mode_sandbox()
    yield


app = FastAPI(
    title="Abyss — Claude Code Multi-Agent Worktree & Code Mode Architecture",
    version="0.2.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ProviderUpdatePayload(BaseModel):
    strategy: str | None = None
    active_single_provider: str | None = None
    role_mapping: dict[str, str] | None = None
    providers: dict[str, dict[str, Any]] | None = None


class WorkflowTriggerPayload(BaseModel):
    goal: str = "Build RepoOnboard: GitLab/GitHub Codebase Onboarding & Socratic Q&A Service"
    engine_mode: str = "agent_runtime"


class RepoOnboardRequest(BaseModel):
    repo_url_or_path: str


class SocraticAnswerRequest(BaseModel):
    repo_url_or_path: str = ""
    question_id: str
    selected_index: int
    reflection: str = ""


class CodeModeRunRequest(BaseModel):
    code: str
    scenario_id: str = "custom"
    reset_sandbox: bool = True
    simulate_slow_hook: bool = False
    hook_on_failure: Literal["block", "continue"] = "block"


# 캐시된 최근 온보딩 분석 패키지
_cached_onboard_pkg: dict[str, Any] | None = None


def _reset_code_mode_sandbox() -> Path:
    """Code Mode(`REPL`) 실습을 위한 격리 샌드박스 Worktree를 초기화합니다."""
    (CODE_MODE_SANDBOX / "src").mkdir(parents=True, exist_ok=True)
    (CODE_MODE_SANDBOX / "docs").mkdir(parents=True, exist_ok=True)
    (CODE_MODE_SANDBOX / "app").mkdir(parents=True, exist_ok=True)

    (CODE_MODE_SANDBOX / "src" / "retry_policy.py").write_text(
        '"""Retry policy configuration for internal LLM gateway calls."""\n\n'
        "def retryPolicy() -> dict:\n"
        '    return {"maxAttempts": 3, "backoffMs": 150, "jitter": True}\n',
        encoding="utf-8",
    )
    (CODE_MODE_SANDBOX / "src" / "test_retry.py").write_text(
        "from src.retry_policy import retryPolicy\n\n"
        "def test_retry_limit_raised_to_five() -> None:\n"
        '    cfg = retryPolicy()\n'
        '    assert cfg["maxAttempts"] == 5, f"Expected 5, got {cfg[\'maxAttempts\']}"\n',
        encoding="utf-8",
    )
    (CODE_MODE_SANDBOX / "docs" / "architecture.md").write_text(
        "# Abyss Harness Docs\n"
        "- Valid link: [retry_policy.py](../src/retry_policy.py)\n"
        "- Broken link 1: [legacy_agent.py](../src/legacy_agent.py)\n",
        encoding="utf-8",
    )
    (CODE_MODE_SANDBOX / "docs" / "runbook.md").write_text(
        "# Intranet Qwen 3.8 Runbook\n"
        "- Valid link: [test_retry.py](../src/test_retry.py)\n"
        "- Broken link 2: [missing_config.yaml](./missing_config.yaml)\n",
        encoding="utf-8",
    )
    return CODE_MODE_SANDBOX


CODE_MODE_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "retry_policy",
        "title": "1. [문서 대표 예제] 검색 → 파일 수정 → 테스트 1-Step 파이프라인 (retryPolicy)",
        "badge": "1 Turn vs 4 Turns",
        "doc_section": "How Code Mode works (code.claude.com/docs/en/code-mode)",
        "claude_js_code": (
            'const files = await $`rg -l "function retryPolicy" src`.text();\n'
            'const target = files.trim().split("\\n")[0];\n'
            'await edit(target, "maxAttempts: 3", "maxAttempts: 5");\n'
            '({ edited: target, tests: $`npm test -- retryPolicy`.nothrow() });'
        ),
        "python_code": (
            'files = (await sh(\'grep -rl "def retryPolicy" src\')).text\n'
            "target = files.strip().splitlines()[0]\n"
            'await edit(target, \'"maxAttempts": 3\', \'"maxAttempts": 5\')\n'
            'tests = await sh("pytest -q src/test_retry.py", nothrow=True)\n'
            '{"edited": target, "tests": {"exit_code": tests.exit_code, "passed": tests.ok, "output": tests.tail(4)}}'
        ),
        "learning_point": (
            "기존 Direct Tool Calling에서는 ① grep 호출 → ② LLM이 결과 읽음 → ③ edit 호출 → ④ pytest 호출까지 "
            "총 4번의 LLM 왕복과 전체 로그 컨텍스트 적재가 필요합니다. Code Mode는 단 1번의 REPL 코드로 실행하고 "
            "마지막 줄의 요약 딕셔너리만 모델에게 반환합니다."
        ),
    },
    {
        "id": "broken_md_links",
        "title": "2. [문서 Try-It 예제] 모든 Markdown 병렬 읽기(gather) & 깨진 로컬 링크만 필터링",
        "badge": "Parallel + Filter",
        "doc_section": "Try Code Mode — Step 4 (Many related tool calls & filtering)",
        "claude_js_code": (
            "// Prompt: Read every Markdown file in this project and list the links\n"
            "// that point to local files that don't exist.\n"
            "const mdFiles = await glob('**/*.md');\n"
            "const texts = await Promise.all(mdFiles.map(f => read(f)));\n"
            "// Filter broken local links in code before returning to Claude..."
        ),
        "python_code": (
            'md_files = await ls(".", "**/*.md")\n'
            "contents = await gather(*[read(p) for p in md_files])\n"
            'existing = set(await ls(".", "**/*"))\n'
            "broken = []\n"
            "for md_path, text in zip(md_files, contents):\n"
            '    for chunk in text.split("](")[1:]:\n'
            '        link = chunk.split(")")[0]\n'
            '        if link.startswith("./") or link.startswith("../"):\n'
            '            norm = link.replace("../", "").replace("./", "docs/")\n'
            "            if norm not in existing:\n"
            '                broken.append({"markdown": md_path, "broken_target": link})\n'
            '{"scanned_files": md_files, "broken_links_count": len(broken), "broken_links": broken}'
        ),
        "learning_point": (
            "When to use Code Mode: '검색 결과를 모델이 읽기 전에 코드로 필터링해야 할 때'와 '독립된 여러 파일을 동시에 읽을 때'. "
            "전체 Markdown 원문을 LLM 컨텍스트에 넣지 않고 코드 안에서 깨진 링크 2건만 추려 반환합니다."
        ),
    },
    {
        "id": "compound_cmd_trap",
        "title": "3. [권한 규칙 함정] 복합 셸 명령 세그먼트 분리 차단 (cd 'app' && git push)",
        "badge": "Permission Gate",
        "doc_section": "Permissions and long-running code — Compound commands",
        "claude_js_code": (
            "// Doc warning: In Code Mode, a `git push` in a folder named `app`\n"
            "// can reach your hook as `cd 'app' && git push`.\n"
            "await $`cd 'app' && git push origin main`;"
        ),
        "python_code": (
            '# 복합 명령어로 앞쪽에 cd나 환경변수를 붙여 Bash(git push*) 규칙 우회를 시도합니다.\n'
            'await sh("cd \'app\' && FOO=1 git push origin main")'
        ),
        "learning_point": (
            "단순 문자열 접두어 매칭(`cmd.startswith('git push')`)은 `cd 'app' && git push`에 뚫립니다. "
            "abyss/core/permissions.py의 `split_compound_command()`는 `&&`, `||`, `;`, `|` 기준으로 세그먼트를 쪼개고 "
            "환경변수 대입을 정규화한 뒤 각 세그먼트를 `Bash(git push*)` deny 규칙과 대조하여 정확히 차단합니다."
        ),
    },
    {
        "id": "runtime_escape_and_hook",
        "title": "4. [Auto Mode 분류기 한계 극복] 런타임 계산 경로 탈출 차단 & PreToolUse Hook",
        "badge": "Runtime Isolation",
        "doc_section": "Permission checks — Auto mode classifier vs runtime values & Hook timeout",
        "claude_js_code": (
            "// Doc: The classifier reviews each piece of code before it runs, not each tool call.\n"
            "// It doesn't see values computed at run time, such as a path taken from search output.\n"
            "const dynamicPath = ['..', '..', 'main_checkout_hack.py'].join('/');\n"
            "await write(dynamicPath, 'escaped');"
        ),
        "python_code": (
            '# 정적 AST 검사(REPL PreToolUse) 시점에는 문자열 리터럴이 보이지 않는 런타임 조합 경로\n'
            'parts = ["..", "..", "escaped_outside_worktree.py"]\n'
            'runtime_target = "/".join(parts)\n'
            'await write(runtime_target, "attempt escape")\n'
            '{"written": runtime_target}'
        ),
        "learning_point": (
            "Auto Mode의 코드 정적 분류기(STEP 0 REPL Review)는 런타임에 계산되는 변수(`runtime_target`)를 볼 수 없어 통과시킵니다. "
            "하지만 STEP 2에서 내부 `Write` 호출이 일어나는 순간 `WorktreeIsolationPolicy`가 실제 해석된 경로를 검증하여 차단합니다."
        ),
    },
]


def _get_repo_onboard_service() -> Any:
    """에이전트들이 Worktree에서 개발해 main 브랜치로 머지한 RepoOnboardService를 로드합니다."""
    if str(DEMO_WORKSPACE) not in sys.path:
        sys.path.insert(0, str(DEMO_WORKSPACE))
    from repo_onboard.service import RepoOnboardService

    return RepoOnboardService()


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_dashboard_state() -> dict[str, Any]:
    state = coordinator.get_dashboard_state()
    state["deepagents_preview"] = create_worktree_deep_agent(
        rotator=rotator,
        agent_id="worker-analyzer",
        role="worker-analyzer",
        worktree_root=DEMO_WORKSPACE / ".abyss" / "worktrees" / "worker-analyzer",
    )
    return state


@app.post("/api/providers/config")
async def update_providers_config(payload: ProviderUpdatePayload) -> dict[str, Any]:
    updated = rotator.update_config(payload.model_dump(exclude_none=True))
    return {"ok": True, "rotator": updated}


@app.post("/api/providers/test/{provider_id}")
async def test_provider(provider_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(rotator.test_provider_connection, provider_id)


@app.post("/api/workflow/run")
async def run_multi_agent_workflow(payload: WorkflowTriggerPayload) -> dict[str, Any]:
    if coordinator.is_running:
        raise HTTPException(status_code=409, detail="Workflow is already running.")
    coordinator.engine_mode = payload.engine_mode
    state = await coordinator.execute_full_workflow(user_goal=payload.goal)
    return state


@app.get("/api/events/stream")
async def stream_events() -> StreamingResponse:
    queue = coordinator.subscribe_events()

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            coordinator.unsubscribe_events(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/learning-core")
async def get_learning_core_files() -> dict[str, Any]:
    """
    발표 및 학습 증빙용:
    [내가 직접 구현·학습할 핵심 코어 5종]의 실제 소스 코드와 아키텍처 포인트를 반환합니다.
    """
    core_files = [
        {
            "id": "agent_runtime",
            "title": "Core #1 · Tool-Use Execution Loop & Context Reset Handoff",
            "file_path": "abyss/core/agent_runtime.py",
            "claude_pattern": "Anthropic Engineering: Context Reset + Structured Handoff Artifact",
            "why_different": (
                "컨텍스트 창이 찼을 때 메시지 히스토리를 요약 압축(Compaction)하면 초기 스키마 제약이 소실되고 조기 종료(Premature Termination)가 발생합니다. "
                "이 런타임은 매 스텝 `<think>` 추론 블록을 분리 추출하고, 턴 종료 시 히스토리를 비운 뒤 수정 파일·차단 횟수·도구 이력만 담은 "
                "`HandoffArtifact`로 다음 에이전트(Reviewer/Lead)의 컨텍스트를 초기화합니다."
            ),
            "ownership": "User Implemented & Tuned (Core Mechanism)",
        },
        {
            "id": "isolation_policy",
            "title": "Core #2 · 4-Check Git Worktree Isolation Policy",
            "file_path": "abyss/core/isolation_policy.py",
            "claude_pattern": "Claude Code Docs: Run parallel sessions with worktrees — 4-Check Enforcement",
            "why_different": (
                "가벼운 Git Worktree(`.abyss/worktrees/<id>`)로 브랜치와 작업 트리를 분리하되, 도구 실행 직전에 "
                "① File Edit Path(`is_relative_to`) ② Command CWD ③ Git Redirect(`git -C`, `--git-dir`, `cd ..`) "
                "④ Dynamic Command Shape(`${!var}`, `eval`, 백틱) 4가지 결정론적 검증을 강제해 메인 체크아웃 오염을 차단합니다."
            ),
            "ownership": "User Implemented & Tuned (Core Mechanism)",
        },
        {
            "id": "evaluator_rubric",
            "title": "Core #3 · Independent 3-Agent Evaluator Rubric",
            "file_path": "abyss/core/evaluator_rubric.py",
            "claude_pattern": "Anthropic Three-Agent Architecture: Separating Generator from Evaluator",
            "why_different": (
                "생성 에이전트(Worker)의 자기 과대평가(Self-evaluation bias)를 차단하기 위해 평가자(Reviewer)를 분리했습니다. "
                "Worktree 브랜치의 실제 `git diff`, 격리된 `pytest` 서브프로세스 결과(exit_code == 0), 동료 에이전트와의 메일박스 스키마 계약을 "
                "독립 채점(80점 이상 & 테스트 통과)하여 `main` 브랜치 머지 게이트로 작동합니다."
            ),
            "ownership": "User Implemented & Tuned (Core Mechanism)",
        },
        {
            "id": "code_mode",
            "title": "Core #4 · Claude Code Experimental Code Mode (`REPL`)",
            "file_path": "abyss/core/code_mode.py",
            "claude_pattern": "Claude Code Docs: Run tool calls from code with Code Mode (`CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1`)",
            "why_different": (
                "도구마다 LLM 왕복을 반복하는 Direct Tool Calling 대신, 모델이 작성한 비동기 코드 1덩어리가 병렬 실행(`gather`), 조건 분기, 출력 필터링을 수행하고 "
                "마지막 표현식만 반환해 라운드트립과 컨텍스트 적재량을 줄입니다. 공식 Claude Code는 Anthropic 직결 API에서만 허용하지만, "
                "이를 파이썬으로 직접 구현해 사내 Qwen 3.8 Flash 엔드포인트에서도 동일하게 구동합니다."
            ),
            "ownership": "User Implemented & Tuned (Core Mechanism)",
        },
        {
            "id": "permissions",
            "title": "Core #5 · Compound Command Permission Splitter (`deny` / `ask`)",
            "file_path": "abyss/core/permissions.py",
            "claude_pattern": "Claude Code Docs: Code Mode Permission Checks & Compound Bash Commands",
            "why_different": (
                "Code Mode 내부에서는 `cd 'app' && FOO=1 git push` 같은 복합 명령이 단일 호출로 들어옵니다. "
                "단순 문자열 접두어 규칙이 우회되지 않도록 따옴표·리다이렉션(`2>&1`) 보존 파서로 복합 명령을 세그먼트 단위로 쪼개어 "
                "`Bash(git push*)`, `Edit(.env)` 등의 `deny`/`ask` 규칙을 개별 세그먼트마다 검증합니다."
            ),
            "ownership": "User Implemented & Tuned (Core Mechanism)",
        },
    ]

    for item in core_files:
        full_path = REPO_ROOT / item["file_path"]
        item["code"] = (
            full_path.read_text(encoding="utf-8") if full_path.exists() else "# File not found"
        )

    infra_files = [
        {
            "file_path": "abyss/orchestrator/worktree_manager.py",
            "role": "Git Worktree 서브프로세스 생성·Lock·Merge 실행기",
            "ownership": "AI Infrastructure Scaffold",
        },
        {
            "file_path": "abyss/orchestrator/team_coordinator.py",
            "role": "공유 태스크 보드(.abyss/tasks.json) & 메일박스(.jsonl) 비동기 코디네이터",
            "ownership": "AI Infrastructure Scaffold",
        },
        {
            "file_path": "abyss/llm/provider_rotator.py",
            "role": "사내 Qwen 3.8 Flash / Gemini / OpenAI / Mock 로테이션 라우터",
            "ownership": "AI Infrastructure Scaffold",
        },
        {
            "file_path": "abyss/orchestrator/deepagents_bridge.py",
            "role": "LangChain DeepAgents (v0.7+) + TodoListMiddleware 연동 브리지",
            "ownership": "AI Infrastructure Scaffold",
        },
    ]

    return {
        "core_modules": core_files,
        "infra_modules": infra_files,
    }


@app.get("/api/code-mode/scenarios")
async def get_code_mode_scenarios() -> dict[str, Any]:
    return {
        "env_flag": "CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1",
        "min_claude_code_version": "v2.1.323 (v2.1.326+ with Agent Teams)",
        "default_permissions": {
            "deny": PermissionRules.default().deny,
            "ask": PermissionRules.default().ask,
        },
        "scenarios": CODE_MODE_SCENARIOS,
    }


@app.post("/api/code-mode/run")
async def run_code_mode_snippet(req: CodeModeRunRequest) -> dict[str, Any]:
    """
    격리된 샌드박스 Worktree에서 Code Mode(`REPL`) 스니펫을 실행하고,
    - 대화창 뷰(개별 Tool Call 및 Edit diff)
    - `Ctrl+O` Transcript Viewer 뷰(접힌 Code 행 + 반환값)
    - 기존 Direct Tool Calling 대비 LLM 왕복 및 컨텍스트 절감 지표
    를 함께 반환합니다.
    """
    if req.reset_sandbox or not CODE_MODE_SANDBOX.exists():
        _reset_code_mode_sandbox()

    hooks: list[HookSpec] = []
    if req.simulate_slow_hook:
        def _slow_pre_tool_hook(_payload: dict[str, Any]) -> dict[str, Any] | None:
            time.sleep(0.25)
            return None

        hooks.append(
            HookSpec(
                name="slow-security-scanner",
                matcher="Bash|Edit|Write",
                fn=_slow_pre_tool_hook,
                timeout_s=0.05,
                on_failure=req.hook_on_failure,
            )
        )

    executor = CodeModeExecutor(
        worktree_root=CODE_MODE_SANDBOX,
        main_checkout_root=DEMO_WORKSPACE,
        permissions=PermissionRules.default(),
        hooks=hooks,
        event_callback=coordinator.emit_event,
        agent_id="code-mode-repl",
    )

    run_res = await executor.arun(req.code)
    res_dict = run_res.to_dict()

    # Direct Mode 대비 효율 비교 지표 계산
    inner_calls_count = max(1, len(run_res.tool_calls))
    raw_tool_chars = sum(len(c.output_preview) + len(c.diff) + 120 for c in run_res.tool_calls)
    returned_chars = max(1, len(run_res.return_text) or len(run_res.error))
    saved_pct = max(0, round((1.0 - min(1.0, returned_chars / max(raw_tool_chars, returned_chars + 1))) * 100))

    return {
        "result": res_dict,
        "comparison": {
            "direct_mode_llm_turns": inner_calls_count + 1,
            "code_mode_llm_turns": 1,
            "inner_tool_calls_executed": len(run_res.tool_calls),
            "raw_intermediate_chars": raw_tool_chars,
            "returned_to_model_chars": returned_chars,
            "context_saved_pct": saved_pct,
        },
    }


@app.post("/api/repo-onboard/analyze")
async def analyze_repository_for_onboarding(req: RepoOnboardRequest) -> dict[str, Any]:
    """
    Worker 1, 2, 3가 Worktree에서 병렬 개발해 머지한 `RepoOnboardService`를 실제로 호출하여
    입력받은 GitLab/GitHub URL 또는 로컬 레포지토리를 분석하고 Archify 그래프 + 소크라테스 문답을 반환합니다.
    """
    global _cached_onboard_pkg
    target = req.repo_url_or_path.strip() or str(REPO_ROOT)
    if target in (".", "abyss", "niisow/abyss", "self"):
        target = str(REPO_ROOT)
    elif target == "demo":
        target = str(DEMO_WORKSPACE)

    try:
        svc = _get_repo_onboard_service()
        pkg = await asyncio.to_thread(svc.onboard_repository, target)
        _cached_onboard_pkg = pkg
        return pkg
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/repo-onboard/answer")
async def evaluate_socratic_answer(req: SocraticAnswerRequest) -> dict[str, Any]:
    global _cached_onboard_pkg
    try:
        svc = _get_repo_onboard_service()
        if _cached_onboard_pkg is None:
            target = req.repo_url_or_path.strip() or str(REPO_ROOT)
            if target in (".", "abyss", "niisow/abyss", "self"):
                target = str(REPO_ROOT)
            _cached_onboard_pkg = await asyncio.to_thread(svc.onboard_repository, target)
        svc._last_package = _cached_onboard_pkg
        return svc.check_socratic_answer(
            question_id=req.question_id,
            selected_index=req.selected_index,
            reflection=req.reflection,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
