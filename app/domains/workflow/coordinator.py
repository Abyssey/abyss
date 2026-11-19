"""
app/domains/workflow/coordinator.py
Agent Teams (Shared Task List + Mailbox) & 3-Worktree Parallel Coordinator.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.core.config import AppSettings
from app.core.llm.provider_rotator import LLMProviderRotator
from app.domains.execution.runtime import AgentRuntime, HandoffArtifact
from app.domains.workflow.deepagents_adapter import create_worktree_deep_agent
from app.domains.workflow.evaluator import EvaluatorRubric, ReviewDecision
from app.domains.workflow.seed_templates import (
    WORKER_1_ANALYZER_CODE,
    WORKER_1_TEST_CODE,
    WORKER_2_TEST_CODE,
    WORKER_2_TUTOR_CODE,
    WORKER_3_TEST_CODE,
    WORKER_3_VIEW_CODE,
)
from app.domains.workflow.worktree_adapter import GitWorktreeManager
from app.domains.workflow.worktree_tools import build_agent_tools


@dataclass
class TeamTask:
    """공유 태스크 보드(.abyss/tasks.json)의 개별 작업 항목."""

    task_id: str
    title: str
    description: str
    assigned_to: str
    worktree_branch: str
    target_files: list[str]
    required_symbols: list[str]
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"  # "pending" | "in_progress" | "in_review" | "approved" | "merged" | "rejected"
    review_score: int = 0
    commit_hash: str = ""
    provider_used: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "assigned_to": self.assigned_to,
            "worktree_branch": self.worktree_branch,
            "target_files": self.target_files,
            "required_symbols": self.required_symbols,
            "depends_on": self.depends_on,
            "status": self.status,
            "review_score": self.review_score,
            "commit_hash": self.commit_hash,
            "provider_used": self.provider_used,
        }


@dataclass
class MailboxMessage:
    """에이전트 팀 메일박스(.abyss/mailbox/*.jsonl) 메시지."""

    msg_id: str
    timestamp: float
    sender: str
    recipient: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
        }


class MultiAgentTeamCoordinator:
    """
    멀티 에이전트 3-Worktree 병렬 개발 오케스트레이터:
    - Lead Agent: 태스크 분해 및 공유 태스크 보드(`.abyss/tasks.json`) 초기화
    - 3 Worktree Workers (`worker-analyzer`, `worker-tutor`, `worker-web`):
      각자 `.abyss/worktrees/<worker_id>` 독립 브랜치에서 병렬 개발,
      `WorktreeIsolationPolicy` 4대 보안 검증 통과,
      `.abyss/mailbox/*.jsonl`로 JSON 스키마 합의 및 커밋
    - Reviewer Agent (`EvaluatorRubric`):
      각 Worktree 브랜치의 `git diff` 및 `pytest`를 독립 실행하여 채점/승인
    - Lead Agent Merge:
      승인된 3개 Worktree 브랜치를 `main`에 순차 머지(`git merge --no-ff`)하고 통합 테스트 및 원격 푸시 수행
    """

    def __init__(
        self,
        workspace_root: str | Path,
        rotator: LLMProviderRotator,
        bare_remote_path: str | Path | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.bare_remote_path = Path(bare_remote_path).resolve() if bare_remote_path else None
        self.settings = settings
        self.abyss_dir = self.workspace_root / ".abyss"
        self.mailbox_dir = self.abyss_dir / "mailbox"
        self.tasks_file = self.abyss_dir / "tasks.json"
        self.rotator = rotator
        self.rubric = EvaluatorRubric()
        self.worktree_mgr = GitWorktreeManager(self.workspace_root)

        self.tasks: dict[str, TeamTask] = {}
        self.mailbox_log: list[MailboxMessage] = []
        self.event_stream: list[dict[str, Any]] = []
        self.handoffs: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.merge_results: list[dict[str, Any]] = []
        self.last_push_result: dict[str, Any] | None = None
        self.engine_mode: str = "agent_runtime"  # "agent_runtime" | "deepagents"
        self.is_running: bool = False
        self.last_status: str = "idle"
        self._listeners: list[asyncio.Queue[dict[str, Any]]] = []

        self._ensure_base_package()
        if self.bare_remote_path is not None:
            self.ensure_internal_bare_remote()

    def ensure_internal_bare_remote(self) -> str:
        """오프라인 데모 및 테스트용 내부 Bare 원격 저장소를 멱등 초기화합니다."""
        target_bare = self.bare_remote_path or (self.workspace_root.parent / "internal_origin.git")
        self.bare_remote_path = Path(target_bare).resolve()
        return self.worktree_mgr.ensure_internal_bare_remote(self.bare_remote_path)

    def _ensure_base_package(self) -> None:
        """데모 워크스페이스에 기본 패키지 초기화 파일과 .worktreeinclude를 준비합니다."""
        self.abyss_dir.mkdir(parents=True, exist_ok=True)
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)

        wt_include = self.workspace_root / ".worktreeinclude"
        if not wt_include.exists():
            wt_include.write_text(".env\n.env.local\n", encoding="utf-8")

        gitignore = self.workspace_root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                ".abyss/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n.env\n.env.local\n",
                encoding="utf-8",
            )

        pkg_dir = self.workspace_root / "repo_onboard"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        init_file = pkg_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text(
                '"""RepoOnboard: Local & Internal Git Codebase Onboarding & Socratic Tutor."""\n',
                encoding="utf-8",
            )

        tests_dir = self.workspace_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        self.worktree_mgr._run_git(
            ["add", ".gitignore", "repo_onboard/__init__.py", ".worktreeinclude"]
        )
        status = self.worktree_mgr._run_git(["status", "--porcelain"]).stdout.strip()
        if status:
            self.worktree_mgr._run_git(
                ["commit", "-m", "chore: initialize repo_onboard package skeleton"]
            )

    def _reset_generated_targets_if_present(self, target_files: list[str]) -> None:
        """
        Reset previously merged demo target files via a forward commit before creating
        fresh worktrees so repeat `POST /api/workflow/run` invocations always produce
        real branch diffs, full 100/100 evaluations, 3 `--no-ff` merges, and fast-forward push.
        """
        existing = [f for f in target_files if (self.workspace_root / f).exists()]
        if not existing:
            return
        self.worktree_mgr._run_git(["rm", "-f", "--ignore-unmatch", "--", *existing])
        status = self.worktree_mgr._run_git(["status", "--porcelain"]).stdout.strip()
        if status:
            self.worktree_mgr._run_git(
                ["commit", "-m", "chore: reset demo targets for fresh 3-worktree parallel build"]
            )

    def subscribe_events(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._listeners.append(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._listeners:
            self._listeners.remove(q)

    def emit_event(self, event: dict[str, Any]) -> None:
        enriched = {"timestamp": time.time(), **event}
        self.event_stream.append(enriched)
        for q in list(self._listeners):
            try:
                q.put_nowait(enriched)
            except Exception:  # noqa: BLE001
                pass

    def _save_tasks(self) -> None:
        data = [t.to_dict() for t in self.tasks.values()]
        self.tasks_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def send_mailbox_message(self, sender: str, recipient: str, content: str) -> str:
        msg = MailboxMessage(
            msg_id=f"msg-{len(self.mailbox_log) + 1:03d}",
            timestamp=time.time(),
            sender=sender,
            recipient=recipient,
            content=content,
        )
        self.mailbox_log.append(msg)
        inbox_file = self.mailbox_dir / f"{recipient}.jsonl"
        with inbox_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")

        self.emit_event(
            {
                "agent_id": sender,
                "role": "teammate",
                "event_type": "mailbox_sent",
                "recipient": recipient,
                "content": content,
            }
        )
        return f"Message delivered to {recipient} ({msg.msg_id})."

    def read_mailbox_messages(self, agent_id: str) -> str:
        inbox_file = self.mailbox_dir / f"{agent_id}.jsonl"
        if not inbox_file.exists():
            return "No messages in inbox."
        lines = inbox_file.read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(line) for line in lines if line.strip()]
        return "\n".join(f"[{m['sender']} -> {m['recipient']}]: {m['content']}" for m in parsed)

    def update_task_status(self, task_id: str, status: str, note: str = "") -> str:
        if task_id in self.tasks:
            self.tasks[task_id].status = status
            self._save_tasks()
            self.emit_event(
                {
                    "agent_id": self.tasks[task_id].assigned_to,
                    "event_type": "task_updated",
                    "task_id": task_id,
                    "status": status,
                    "note": note,
                }
            )
        return f"Task {task_id} status updated to '{status}'."

    def _build_worker_scripted_responder(
        self,
        worker_id: str,
        code_path: str,
        code_content: str,
        test_path: str,
        test_content: str,
        peer_recipient: str,
        schema_message: str,
        demonstrate_guard_block: bool = False,
    ) -> Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]:
        turn_idx = 0

        def responder(messages: list[dict[str, Any]], tools_schema: list[dict[str, Any]]) -> dict[str, Any]:
            nonlocal turn_idx
            turn_idx += 1

            if turn_idx == 1 and demonstrate_guard_block:
                return {
                    "content": (
                        f"<think>[{worker_id}] 먼저 메인 체크아웃의 외부 git 상태를 직접 조회해 보겠습니다.</think>\n"
                        "Checking repository state before writing module..."
                    ),
                    "tool_calls": [
                        {
                            "id": f"call_{worker_id}_guard_demo",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps(
                                    {"command": f"git -C {self.workspace_root} status"}
                                ),
                            },
                        }
                    ],
                }

            step_offset = 1 if demonstrate_guard_block else 0

            if turn_idx == 1 + step_offset:
                return {
                    "content": (
                        f"<think>[{worker_id}] Worktree 격리 정책에 맞춰 현재 할당된 Worktree 내부 경로(`{code_path}`, `{test_path}`)에 "
                        "구현 코드와 단위 테스트를 작성합니다.</think>\n"
                        f"Implementing `{code_path}` and unit tests `{test_path}` in isolated worktree."
                    ),
                    "tool_calls": [
                        {
                            "id": f"call_{worker_id}_write_code",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": code_path, "content": code_content}
                                ),
                            },
                        },
                        {
                            "id": f"call_{worker_id}_write_test",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": test_path, "content": test_content}
                                ),
                            },
                        },
                    ],
                }

            if turn_idx == 2 + step_offset:
                return {
                    "content": (
                        f"<think>[{worker_id}] 동료 에이전트(`{peer_recipient}`)에게 데이터 계약(JSON Schema)을 "
                        "메일박스로 공유하고, Worktree 내에서 pytest를 실행해 검증합니다.</think>\n"
                        f"Syncing contract via Agent Mailbox with `{peer_recipient}` and running pytest."
                    ),
                    "tool_calls": [
                        {
                            "id": f"call_{worker_id}_mailbox",
                            "type": "function",
                            "function": {
                                "name": "send_team_message",
                                "arguments": json.dumps(
                                    {"recipient": peer_recipient, "message": schema_message}
                                ),
                            },
                        },
                        {
                            "id": f"call_{worker_id}_pytest",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps(
                                    {"command": f"pytest {test_path} -q"}
                                ),
                            },
                        },
                    ],
                }

            return {
                "content": (
                    f"<think>[{worker_id}] 모든 단위 테스트가 통과되었고 메일박스 스키마 공유도 완료했습니다. "
                    "독립 Reviewer 인계를 위해 구조화된 Handoff 요약을 반환합니다.</think>\n"
                    f"Completed `{code_path}` implementation and verified `{test_path}` in isolated worktree."
                ),
                "tool_calls": [],
            }

        return responder

    async def _run_single_worker(
        self,
        task: TeamTask,
        code_path: str,
        code_content: str,
        test_path: str,
        test_content: str,
        peer_recipient: str,
        schema_message: str,
        demonstrate_guard_block: bool = False,
    ) -> HandoffArtifact:
        worker_id = task.assigned_to
        wt_info = self.worktree_mgr.worktrees[worker_id]
        task.status = "in_progress"
        self._save_tasks()

        self.emit_event(
            {
                "agent_id": worker_id,
                "role": "worker",
                "branch": wt_info.branch_name,
                "event_type": "worker_started",
                "task_id": task.task_id,
                "worktree_path": str(wt_info.worktree_path),
            }
        )

        registry, schemas = build_agent_tools(
            worktree_root=wt_info.worktree_path,
            agent_id=worker_id,
            send_mailbox_fn=self.send_mailbox_message,
            read_mailbox_fn=self.read_mailbox_messages,
            update_task_fn=self.update_task_status,
        )

        runtime = AgentRuntime(
            agent_id=worker_id,
            role="worker",
            worktree_root=wt_info.worktree_path,
            main_checkout_root=self.workspace_root,
            worktree_branch=wt_info.branch_name,
            tools_registry=registry,
            tools_schema=schemas,
            max_steps=8,
            event_callback=self.emit_event,
        )

        scripted_responder = self._build_worker_scripted_responder(
            worker_id=worker_id,
            code_path=code_path,
            code_content=code_content,
            test_path=test_path,
            test_content=test_content,
            peer_recipient=peer_recipient,
            schema_message=schema_message,
            demonstrate_guard_block=demonstrate_guard_block,
        )

        def llm_bridge(
            messages: list[dict[str, Any]], tools_schema: list[dict[str, Any]]
        ) -> dict[str, Any]:
            step_reply = scripted_responder(messages, tools_schema)
            rot_res = self.rotator.invoke_chat(
                agent_id=worker_id,
                role=worker_id,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a concise coding subagent working in an isolated git worktree.",
                    },
                    {
                        "role": "user",
                        "content": f"Briefly state your next engineering action for {task.title} in 1 sentence.",
                    },
                ],
                tools_schema=None,
                mock_responder=lambda _m, _t: step_reply,
            )
            provider_tag = rot_res.get("provider", "mock:simulator")
            live_note = (rot_res.get("content") or "").strip()
            merged_content = step_reply["content"]
            if live_note and live_note not in merged_content and not provider_tag.startswith("mock:"):
                merged_content = f"<think>[Live LLM ({provider_tag})]: {live_note}</think>\n{merged_content}"
            return {
                "content": merged_content,
                "tool_calls": step_reply["tool_calls"],
                "provider": provider_tag,
            }

        handoff = await asyncio.to_thread(
            runtime.run,
            system_prompt=f"You are {worker_id} isolated in git worktree {wt_info.worktree_path}.",
            task_prompt=task.description,
            llm_callable=llm_bridge,
        )

        commit_hash = await asyncio.to_thread(
            self.worktree_mgr.commit_worktree_changes,
            worker_id,
            f"feat({worker_id}): implement {task.title}",
        )
        task.commit_hash = commit_hash
        task.provider_used = handoff.provider_used
        task.status = "in_review"
        self._save_tasks()
        self.handoffs[worker_id] = handoff.to_dict()

        return handoff

    async def execute_full_workflow(
        self, user_goal: str = "Build RepoOnboard: Local & Internal Git Codebase Onboarding Service"
    ) -> dict[str, Any]:
        """4단계 멀티 에이전트 병렬 Worktree 파이프라인을 실행합니다."""
        self.is_running = True
        self.last_status = "running"
        self.event_stream.clear()
        self.mailbox_log.clear()
        self.handoffs.clear()
        self.reviews.clear()
        self.merge_results.clear()

        try:
            lead_rot = self.rotator.invoke_chat(
                agent_id="lead",
                role="lead",
                messages=[
                    {
                        "role": "user",
                        "content": f"Decompose '{user_goal}' into 3 parallel git worktree tasks in 1 short sentence.",
                    }
                ],
                mock_responder=lambda _m, _t: {
                    "content": (
                        "<think>[Lead Agent] 충돌 없이 병렬 개발하기 위해 "
                        "① AST/Archify 분석기(worker-analyzer), ② 소크라테스 문답 튜터(worker-tutor), "
                        "③ 통합 온보딩 서비스(worker-web) 3개 Worktree로 분리합니다.</think>\n"
                        "Decomposed into 3 isolated git worktree tasks with mailbox schema contracts."
                    ),
                    "tool_calls": [],
                },
            )
            self.emit_event(
                {
                    "agent_id": "lead",
                    "role": "lead",
                    "event_type": "lead_planned",
                    "provider": lead_rot.get("provider", ""),
                    "goal": user_goal,
                    "message": lead_rot.get("content", ""),
                }
            )

            task_defs = [
                TeamTask(
                    task_id="TASK-1",
                    title="AST & Archify Graph Analyzer (repo_onboard/analyzer.py)",
                    description="Implement RepoAnalyzer to parse repository AST and emit archify IR graph + Mermaid.",
                    assigned_to="worker-analyzer",
                    worktree_branch="worktree-worker-analyzer",
                    target_files=["repo_onboard/analyzer.py", "tests/test_analyzer.py"],
                    required_symbols=["class RepoAnalyzer", "def analyze_target", "archify_graph"],
                ),
                TeamTask(
                    task_id="TASK-2",
                    title="Socratic Onboarding Tutor (repo_onboard/onboarding_tutor.py)",
                    description="Implement SocraticOnboardingTutor for onboarding brief and 3-stage Socratic Q&A.",
                    assigned_to="worker-tutor",
                    worktree_branch="worktree-worker-tutor",
                    target_files=["repo_onboard/onboarding_tutor.py", "tests/test_onboarding_tutor.py"],
                    required_symbols=["class SocraticOnboardingTutor", "def build_onboarding_package", "def evaluate_answer"],
                ),
                TeamTask(
                    task_id="TASK-3",
                    title="Unified RepoOnboard Service (repo_onboard/service.py)",
                    description="Integrate RepoAnalyzer and SocraticOnboardingTutor into RepoOnboardService.",
                    assigned_to="worker-web",
                    worktree_branch="worktree-worker-web",
                    target_files=["repo_onboard/service.py", "tests/test_service.py"],
                    required_symbols=["class RepoOnboardService", "def onboard_repository", "def check_socratic_answer"],
                    depends_on=["TASK-1", "TASK-2"],
                ),
            ]

            self.tasks = {t.task_id: t for t in task_defs}
            self._save_tasks()

            # Ensure repeat workflow runs start from a clean target baseline on `main`
            # (forward commit preserves history and keeps `git push origin main` fast-forwardable).
            self._reset_generated_targets_if_present(
                [fpath for t in task_defs for fpath in t.target_files]
            )

            for t in task_defs:
                wt = self.worktree_mgr.create_worktree(t.assigned_to)
                self.emit_event(
                    {
                        "agent_id": "lead",
                        "role": "lead",
                        "event_type": "worktree_created",
                        "worker_id": t.assigned_to,
                        "branch": wt.branch_name,
                        "worktree_path": str(wt.worktree_path),
                        "locked": wt.locked,
                    }
                )

            await asyncio.gather(
                self._run_single_worker(
                    task=self.tasks["TASK-1"],
                    code_path="repo_onboard/analyzer.py",
                    code_content=WORKER_1_ANALYZER_CODE,
                    test_path="tests/test_analyzer.py",
                    test_content=WORKER_1_TEST_CODE,
                    peer_recipient="worker-tutor",
                    schema_message=(
                        "[SCHEMA CONTRACT] `RepoAnalyzer.analyze_target()` returns dict with keys: "
                        "`repo_name`, `purpose_summary`, `entry_points`, `modules`, and `archify_graph` (`nodes`, `edges`)."
                    ),
                    demonstrate_guard_block=True,
                ),
                self._run_single_worker(
                    task=self.tasks["TASK-2"],
                    code_path="repo_onboard/onboarding_tutor.py",
                    code_content=WORKER_2_TUTOR_CODE,
                    test_path="tests/test_onboarding_tutor.py",
                    test_content=WORKER_2_TEST_CODE,
                    peer_recipient="worker-web",
                    schema_message=(
                        "[SCHEMA CONTRACT] `SocraticOnboardingTutor.build_onboarding_package(analysis)` "
                        "accepts `RepoAnalyzer` output and returns `onboarding_brief` + `socratic_ladder` (3 stages)."
                    ),
                    demonstrate_guard_block=False,
                ),
            )

            wt3_info = self.worktree_mgr.worktrees["worker-web"]
            self.worktree_mgr._run_git(
                ["merge", "--no-ff", "worktree-worker-analyzer", "-m", "chore: sync analyzer branch"],
                cwd=wt3_info.worktree_path,
            )
            self.worktree_mgr._run_git(
                ["merge", "--no-ff", "worktree-worker-tutor", "-m", "chore: sync tutor branch"],
                cwd=wt3_info.worktree_path,
            )

            await self._run_single_worker(
                task=self.tasks["TASK-3"],
                code_path="repo_onboard/service.py",
                code_content=WORKER_3_VIEW_CODE,
                test_path="tests/test_service.py",
                test_content=WORKER_3_TEST_CODE,
                peer_recipient="lead",
                schema_message=(
                    "[READY FOR REVIEW] Integrated `RepoAnalyzer` + `SocraticOnboardingTutor` into "
                    "`RepoOnboardService` (`onboard_repository`, `check_socratic_answer`). All tests green."
                ),
                demonstrate_guard_block=False,
            )

            # Phase 3: Independent Reviewer Agent (`EvaluatorRubric`)
            for t in task_defs:
                worker_id = t.assigned_to
                wt_info = self.worktree_mgr.worktrees[worker_id]
                diff_text = self.worktree_mgr.get_worktree_diff(worker_id)

                test_file = t.target_files[1]
                env = os.environ.copy()
                env["PYTHONPATH"] = str(wt_info.worktree_path)
                pytest_proc = subprocess.run(
                    [sys.executable, "-m", "pytest", test_file, "-v"],
                    cwd=str(wt_info.worktree_path),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                pytest_out = (pytest_proc.stdout or "") + (pytest_proc.stderr or "")
                sent_count = sum(1 for m in self.mailbox_log if m.sender == worker_id)

                decision: ReviewDecision = self.rubric.evaluate_worktree_submission(
                    worker_id=worker_id,
                    branch_name=wt_info.branch_name,
                    git_diff=diff_text,
                    pytest_exit_code=pytest_proc.returncode,
                    pytest_output=pytest_out,
                    required_symbols=t.required_symbols,
                    mailbox_messages_sent=sent_count,
                )

                t.review_score = decision.total_score
                t.status = "approved" if decision.approved else "rejected"
                self._save_tasks()
                self.reviews[worker_id] = {
                    **decision.to_dict(),
                    "diff": diff_text,
                    "pytest_output": pytest_out.strip(),
                }

                self.send_mailbox_message("reviewer", worker_id, decision.feedback_for_worker)
                self.emit_event(
                    {
                        "agent_id": "reviewer",
                        "role": "reviewer",
                        "event_type": "review_completed",
                        "worker_id": worker_id,
                        "branch": wt_info.branch_name,
                        "approved": decision.approved,
                        "score": decision.total_score,
                    }
                )

            # Phase 4: Lead Agent Sequential Git Merge onto `main`
            for t in task_defs:
                worker_id = t.assigned_to
                if t.status != "approved":
                    continue
                merge_res = self.worktree_mgr.merge_worktree_branch(worker_id)
                self.merge_results.append(merge_res)
                if merge_res["merged"]:
                    t.status = "merged"
                    self._save_tasks()
                    self.emit_event(
                        {
                            "agent_id": "lead",
                            "role": "lead",
                            "event_type": "branch_merged",
                            "worker_id": worker_id,
                            "branch": merge_res["branch"],
                            "main_head": merge_res["main_head"],
                        }
                    )

            main_env = os.environ.copy()
            main_env["PYTHONPATH"] = str(self.workspace_root)
            final_pytest = subprocess.run(
                [sys.executable, "-m", "pytest", "tests", "-q"],
                cwd=str(self.workspace_root),
                env=main_env,
                capture_output=True,
                text=True,
                check=False,
            )

            # Push merged `main` to internal origin remote if configured
            remote_info = self.worktree_mgr.get_remote_info()
            if remote_info["configured"]:
                self.last_push_result = await asyncio.to_thread(
                    self.worktree_mgr.push_branch,
                    self.worktree_mgr.get_default_branch(),
                    settings=self.settings,
                )

            self.last_status = "completed"
            self.is_running = False
            self.emit_event(
                {
                    "agent_id": "lead",
                    "role": "lead",
                    "event_type": "workflow_finished",
                    "all_merged": all(t.status == "merged" for t in self.tasks.values()),
                    "integration_pytest_output": (final_pytest.stdout or "").strip(),
                }
            )
            return self.get_dashboard_state()
        except Exception:
            self.last_status = "failed"
            raise
        finally:
            self.is_running = False

    def get_dashboard_state(self) -> dict[str, Any]:
        status = (
            "running"
            if self.is_running
            else ("completed" if (self.last_status == "completed" or self.merge_results) else self.last_status)
        )
        merged_branches = [m["branch"] for m in self.merge_results if m.get("merged")]
        remote_info = self.worktree_mgr.get_remote_info()
        if self.last_push_result is not None:
            remote_info = {**remote_info, "last_push": self.last_push_result}

        return {
            "status": status,
            "is_running": self.is_running,
            "engine_mode": self.engine_mode,
            "workspace_root": str(self.workspace_root),
            "tasks": [t.to_dict() for t in self.tasks.values()],
            "worktrees": {
                wid: info.to_dict() for wid, info in self.worktree_mgr.worktrees.items()
            },
            "mailbox": [m.to_dict() for m in self.mailbox_log],
            "handoffs": self.handoffs,
            "reviews": self.reviews,
            "merges": self.merge_results,
            "merged_branches": merged_branches,
            "internal_remote": remote_info,
            "git_graph": self.worktree_mgr.get_git_graph_log(limit=18),
            "rotator": self.rotator.get_state(),
            "deepagents_preview": create_worktree_deep_agent(
                rotator=self.rotator,
                agent_id="worker-analyzer",
                role="worker-analyzer",
                worktree_root=self.workspace_root / ".abyss" / "worktrees" / "worker-analyzer",
            ),
            "recent_events": self.event_stream[-60:],
        }
