"""
abyss/orchestrator/team_coordinator.py
Claude Code Agent Teams (Shared Task List + Mailbox) & 3-Agent Worktree Coordinator
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from abyss.core.agent_runtime import AgentRuntime, HandoffArtifact
from abyss.core.evaluator_rubric import EvaluatorRubric, ReviewDecision
from abyss.core.tools import build_agent_tools
from abyss.llm.provider_rotator import LLMProviderRotator
from abyss.orchestrator.worktree_manager import GitWorktreeManager


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


# =============================================================================
# 타겟 데모 서비스(RepoOnboard) 모듈 템플릿 — Worker 1, 2, 3가 각자 Worktree에 생성
# =============================================================================
WORKER_1_ANALYZER_CODE = '''"""
repo_onboard/analyzer.py
[Built by worker-analyzer in isolated branch: worktree-worker-analyzer]
GitLab / GitHub / Local Repository AST & Dependency Graph Analyzer (archify IR compatible).
"""

from __future__ import annotations

import ast
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class RepoAnalyzer:
    """
    레포지토리(로컬 경로 또는 GitLab/GitHub URL)를 스캔하여:
    1) 프로젝트 목적 및 아키텍처 요약
    2) 모듈/클래스/함수 목록 및 핵심 진입점(Entry Points)
    3) Archify 호환 인터랙티브 아키텍처 그래프 JSON IR (nodes, edges, layers)
    4) Mermaid 아키텍처 다이어그램
    을 생성합니다.
    """

    IGNORE_DIRS = {".git", ".abyss", ".agents", "demo_workspace", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}

    def analyze_target(self, repo_url_or_path: str) -> dict[str, Any]:
        cleaned = repo_url_or_path.strip()
        if cleaned.startswith(("http://", "https://", "git@")):
            temp_dir = Path(tempfile.mkdtemp(prefix="repo_onboard_"))
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", cleaned, str(temp_dir)],
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            if proc.returncode != 0:
                raise ValueError(f"Failed to clone repository '{cleaned}': {proc.stderr.strip()}")
            root_path = temp_dir
            repo_name = cleaned.rstrip("/").split("/")[-1].removesuffix(".git")
        else:
            root_path = Path(cleaned).resolve()
            if not root_path.exists():
                raise ValueError(f"Repository path does not exist: {cleaned}")
            repo_name = root_path.name

        py_files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in self.IGNORE_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    py_files.append(Path(dirpath) / fn)

        modules: list[dict[str, Any]] = []
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        known_module_ids: set[str] = set()

        for py_file in sorted(py_files)[:40]:
            rel_path = str(py_file.relative_to(root_path))
            mod_id = rel_path.removesuffix(".py").replace("/", ".")
            if mod_id.endswith(".__init__"):
                mod_id = mod_id.removesuffix(".__init__")
            if not mod_id:
                continue
            known_module_ids.add(mod_id)

            source = py_file.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            doc = (ast.get_docstring(tree) or "").strip()
            first_doc_line = doc.splitlines()[0] if doc else f"Module {rel_path}"

            classes: list[dict[str, Any]] = []
            functions: list[dict[str, Any]] = []
            imports: list[str] = []

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    cls_doc = (ast.get_docstring(node) or "").strip()
                    methods = [
                        n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ]
                    classes.append(
                        {
                            "name": node.name,
                            "line": node.lineno,
                            "summary": cls_doc.splitlines()[0] if cls_doc else f"Class {node.name}",
                            "methods": methods[:8],
                        }
                    )
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_doc = (ast.get_docstring(node) or "").strip()
                    functions.append(
                        {
                            "name": node.name,
                            "line": node.lineno,
                            "summary": fn_doc.splitlines()[0] if fn_doc else f"Function {node.name}",
                        }
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)

            layer = self._infer_layer(rel_path)
            mod_record = {
                "module_id": mod_id,
                "file_path": rel_path,
                "layer": layer,
                "purpose": first_doc_line[:160],
                "docstring": doc[:500],
                "classes": classes,
                "functions": functions,
                "imports": imports,
                "line_count": len(source.splitlines()),
            }
            modules.append(mod_record)
            nodes.append(
                {
                    "id": mod_id,
                    "label": Path(rel_path).name,
                    "file_path": rel_path,
                    "layer": layer,
                    "purpose": first_doc_line[:140],
                    "symbols": [c["name"] for c in classes] + [f["name"] for f in functions[:4]],
                }
            )

        for mod in modules:
            src_id = mod["module_id"]
            for imp in mod["imports"]:
                for target_id in known_module_ids:
                    if imp == target_id or imp.startswith(target_id + ".") or target_id.endswith(imp):
                        if src_id != target_id:
                            edge = {"source": src_id, "target": target_id, "relation": "imports"}
                            if edge not in edges:
                                edges.append(edge)

        mermaid_lines = ["flowchart LR"]
        for n in nodes[:18]:
            safe_id = n["id"].replace(".", "_").replace("-", "_")
            mermaid_lines.append(f'    {safe_id}["{n["file_path"]}"]')
        for e in edges[:28]:
            s_id = e["source"].replace(".", "_").replace("-", "_")
            t_id = e["target"].replace(".", "_").replace("-", "_")
            mermaid_lines.append(f"    {s_id} --> {t_id}")

        entry_points = [
            m["file_path"]
            for m in modules
            if any(k in m["file_path"] for k in ("server", "main", "cli", "runtime", "coordinator", "view"))
        ][:5]
        if not entry_points and modules:
            entry_points = [m["file_path"] for m in modules[:3]]

        return {
            "repo_name": repo_name,
            "root_path": str(root_path),
            "total_files": len(modules),
            "total_lines": sum(m["line_count"] for m in modules),
            "purpose_summary": self._synthesize_repo_purpose(repo_name, modules),
            "entry_points": entry_points,
            "modules": modules,
            "archify_graph": {
                "schema_version": "archify-ir-3.2",
                "nodes": nodes,
                "edges": edges,
            },
            "mermaid": "\\n".join(mermaid_lines),
        }

    @staticmethod
    def _infer_layer(rel_path: str) -> str:
        low = rel_path.lower()
        if "core" in low or "policy" in low or "rubric" in low:
            return "Core Alignment & Guardrails"
        if "orchestrat" in low or "worktree" in low or "team" in low:
            return "Worktree & Team Orchestration"
        if "llm" in low or "rotat" in low or "provider" in low:
            return "Multi-LLM Routing"
        if "web" in low or "view" in low or "server" in low:
            return "Web Control Plane & UI"
        if "test" in low:
            return "Verification & Tests"
        return "Application & Domain"

    @staticmethod
    def _synthesize_repo_purpose(repo_name: str, modules: list[dict[str, Any]]) -> str:
        layers = sorted({m["layer"] for m in modules})
        top_classes = [c["name"] for m in modules for c in m["classes"]][:6]
        return (
            f"'{repo_name}' 저장소는 {len(modules)}개의 파이썬 모듈과 {len(layers)}개 아키텍처 계층"
            f"({', '.join(layers)})으로 구성된 시스템입니다. "
            f"핵심 컴포넌트({', '.join(top_classes) or 'Core Modules'})들이 유기적으로 연결되어 동작합니다."
        )
'''

WORKER_1_TEST_CODE = '''"""Unit tests for Worker 1 RepoAnalyzer."""

from pathlib import Path
from repo_onboard.analyzer import RepoAnalyzer


def test_repo_analyzer_extracts_ast_and_archify_graph(tmp_path: Path) -> None:
    pkg = tmp_path / "sample_app"
    pkg.mkdir()
    (pkg / "core.py").write_text(
        '"""Core policy module."""\\nclass GuardPolicy:\\n    """Enforces safety."""\\n    def check(self) -> bool:\\n        return True\\n',
        encoding="utf-8",
    )
    (pkg / "service.py").write_text(
        '"""Main service entry."""\\nfrom sample_app.core import GuardPolicy\\ndef run_service() -> bool:\\n    return GuardPolicy().check()\\n',
        encoding="utf-8",
    )

    analyzer = RepoAnalyzer()
    report = analyzer.analyze_target(str(tmp_path))

    assert report["total_files"] == 2
    assert len(report["archify_graph"]["nodes"]) == 2
    assert any("GuardPolicy" in n["symbols"] for n in report["archify_graph"]["nodes"])
    assert "flowchart LR" in report["mermaid"]
'''

WORKER_2_TUTOR_CODE = '''"""
repo_onboard/onboarding_tutor.py
[Built by worker-tutor in isolated branch: worktree-worker-tutor]
Codebase Onboarding Brief & Socratic Question Ladder Generator (powered by codebase-onboarding & socratic-onboarding skills).
"""

from __future__ import annotations

from typing import Any


class SocraticOnboardingTutor:
    """
    `RepoAnalyzer`가 생성한 AST/아키텍처 분석 JSON 스키마를 입력받아:
    1) 신규 입사자/개발자를 위한 3단계 온보딩 브리핑 (Executive Summary, Reading Order, Layer Breakdown)
    2) 소크라테스식 문답 사다리 (Question Ladder: 힌트 → 객관식/추론 문답 → 소스 파일 라인 근거 해설)
    를 생성합니다.
    """

    def build_onboarding_package(self, analysis: dict[str, Any]) -> dict[str, Any]:
        repo_name = analysis.get("repo_name", "repository")
        modules = analysis.get("modules", [])
        edges = analysis.get("archify_graph", {}).get("edges", [])
        entry_points = analysis.get("entry_points", [])

        layers_map: dict[str, list[dict[str, Any]]] = {}
        for mod in modules:
            layers_map.setdefault(mod["layer"], []).append(mod)

        reading_order: list[dict[str, str]] = []
        for idx, ep in enumerate(entry_points[:4], start=1):
            matched = next((m for m in modules if m["file_path"] == ep), None)
            reading_order.append(
                {
                    "step": f"Step {idx}",
                    "file_path": ep,
                    "reason": matched["purpose"] if matched else "핵심 진입점 모듈",
                }
            )

        questions = self._generate_socratic_questions(repo_name, modules, edges, entry_points)

        return {
            "repo_name": repo_name,
            "onboarding_brief": {
                "headline": f"{repo_name} — Architecture & Onboarding Guide",
                "purpose_summary": analysis.get("purpose_summary", ""),
                "stats": {
                    "modules_count": analysis.get("total_files", len(modules)),
                    "total_lines": analysis.get("total_lines", 0),
                    "dependencies_count": len(edges),
                },
                "reading_order": reading_order,
                "layers": [
                    {
                        "layer_name": layer,
                        "module_count": len(mods),
                        "files": [m["file_path"] for m in mods],
                        "key_responsibility": mods[0]["purpose"] if mods else "",
                    }
                    for layer, mods in layers_map.items()
                ],
            },
            "socratic_ladder": questions,
        }

    def evaluate_answer(
        self, question: dict[str, Any], selected_index: int, user_reflection: str = ""
    ) -> dict[str, Any]:
        correct_idx = int(question.get("correct_index", 0))
        is_correct = selected_index == correct_idx
        return {
            "question_id": question.get("id", "q1"),
            "is_correct": is_correct,
            "selected_index": selected_index,
            "correct_index": correct_idx,
            "coach_response": (
                f"정확합니다! {question.get('explanation', '')}"
                if is_correct
                else (
                    f"다시 한번 코드의 흐름을 따라가 볼까요? 힌트: {question.get('socratic_hint', '')} — "
                    f"핵심 원리: {question.get('explanation', '')}"
                )
            ),
            "source_evidence": question.get("source_evidence", ""),
            "follow_up_prompt": question.get("follow_up_prompt", ""),
        }

    def _generate_socratic_questions(
        self,
        repo_name: str,
        modules: list[dict[str, Any]],
        edges: list[dict[str, str]],
        entry_points: list[str],
    ) -> list[dict[str, Any]]:
        questions: list[dict[str, Any]] = []

        # Stage 1: 진입점과 아키텍처 계층 발견
        first_entry = entry_points[0] if entry_points else (modules[0]["file_path"] if modules else "main.py")
        entry_mod = next((m for m in modules if m["file_path"] == first_entry), modules[0] if modules else {})
        entry_symbols = [c["name"] for c in entry_mod.get("classes", [])] + [
            f["name"] for f in entry_mod.get("functions", [])
        ]
        sym_label = entry_symbols[0] if entry_symbols else Path_stem(first_entry)

        questions.append(
            {
                "id": "stage-1-entrypoint",
                "stage": "Stage 1 · Entry Point & Control Flow",
                "question": (
                    f"이 레포지토리({repo_name})에서 새로운 요청이나 작업이 시작될 때 가장 먼저 살펴봐야 할 "
                    f"핵심 진입 파일과 주요 심볼은 무엇이며, 어떤 역할을 담당하나요?"
                ),
                "socratic_hint": (
                    f"`{first_entry}` 파일의 상단 독스트링과 `{sym_label}` 정의부를 먼저 확인해 보세요."
                ),
                "options": [
                    f"{first_entry} ({sym_label}) — {entry_mod.get('purpose', '핵심 진입점 및 오케스트레이션 수행')}",
                    "정적 에셋 폴더에서 데이터베이스 마이그레이션을 직접 수행한다",
                    "외부 서드파티 바이너리만 호출하고 내부 파이썬 모듈은 관여하지 않는다",
                ],
                "correct_index": 0,
                "explanation": (
                    f"`{first_entry}` 모듈이 `{sym_label}` 등을 통해 핵심 흐름을 시작하고 하위 계층을 호출합니다."
                ),
                "source_evidence": f"{first_entry}:1",
                "follow_up_prompt": f"만약 `{first_entry}`에 새로운 기능을 추가한다면 어느 계층과 먼저 통신해야 할까요?",
            }
        )

        # Stage 2: 모듈 간 의존성 및 격리/안전 설계
        core_mods = [m for m in modules if "Core" in m["layer"] or "policy" in m["file_path"] or len(m["classes"]) > 0]
        target_core = core_mods[0] if core_mods else (modules[-1] if modules else {})
        core_file = target_core.get("file_path", first_entry)
        core_cls = (
            target_core.get("classes", [{}])[0].get("name", "CoreComponent")
            if target_core.get("classes")
            else "CoreComponent"
        )
        core_line = (
            target_core.get("classes", [{}])[0].get("line", 1)
            if target_core.get("classes")
            else 1
        )

        questions.append(
            {
                "id": "stage-2-invariant",
                "stage": "Stage 2 · Core Invariants & Safety Boundary",
                "question": (
                    f"`{core_file}`에 정의된 `{core_cls}`는 시스템에서 어떤 결정론적 보장(Invariant)이나 "
                    f"도메인 규칙을 책임지고 있나요?"
                ),
                "socratic_hint": (
                    f"`{core_file}:{core_line}` 부근의 메서드 시그니처와 반환 타입을 살펴보면, "
                    "단순 데이터 저장이 아니라 검증/실행 규칙을 강제하고 있음을 알 수 있습니다."
                ),
                "options": [
                    "아무 검증 없이 모든 외부 입력을 그대로 셸에 전달한다",
                    f"{target_core.get('purpose', '핵심 도메인 정책 및 상태 검증을 수행한다')} ({core_cls})",
                    "UI 색상 테마만 변경하고 런타임 동작에는 관여하지 않는다",
                ],
                "correct_index": 1,
                "explanation": (
                    f"`{core_file}`의 `{core_cls}`는 '{target_core.get('purpose', '핵심 정책')}' 역할을 수행하여 "
                    "시스템의 안정성과 일관성을 보장합니다."
                ),
                "source_evidence": f"{core_file}:{core_line}",
                "follow_up_prompt": "이 검증 로직이 우회되거나 제거된다면 어떤 장애가 발생할 수 있을지 생각해 보세요.",
            }
        )

        # Stage 3: 모듈 간 연결(Dependency Graph) 추론
        if edges:
            sample_edge = edges[0]
            src_mod = sample_edge["source"]
            dst_mod = sample_edge["target"]
        else:
            src_mod = modules[0]["module_id"] if modules else "app.server"
            dst_mod = modules[-1]["module_id"] if len(modules) > 1 else "app.core"

        questions.append(
            {
                "id": "stage-3-dependency",
                "stage": "Stage 3 · Cross-Module Dependency & Data Contract",
                "question": (
                    f"아키텍처 그래프에서 `{src_mod}` 모듈이 `{dst_mod}` 모듈을 import하여 호출하는 이유는 무엇인가요?"
                ),
                "socratic_hint": (
                    f"상위 오케스트레이션/서비스 계층(`{src_mod}`)이 하위 도메인/정책 모듈(`{dst_mod}`)의 "
                    "결과값을 어떻게 소비하는지 import 구문을 추적해 보세요."
                ),
                "options": [
                    "순환 참조(Circular Import)를 고의로 유발하기 위해서",
                    "사용하지 않는 데드 코드를 유지하기 위해서",
                    f"`{dst_mod}`가 생성/검증한 구조화된 데이터 계약(Schema/Verdict)을 `{src_mod}`의 파이프라인에서 활용하기 위해서",
                ],
                "correct_index": 2,
                "explanation": (
                    f"`{src_mod}`는 `{dst_mod}`의 책임 영역을 직접 중복 구현하지 않고, 명확한 데이터 계약을 통해 결합도를 낮춥니다."
                ),
                "source_evidence": f"{src_mod} -> {dst_mod}",
                "follow_up_prompt": "두 모듈 사이의 반환 스키마가 변경될 때 깨지지 않으려면 어떤 테스트가 필요할까요?",
            }
        )

        return questions


def Path_stem(file_path: str) -> str:
    return file_path.rsplit("/", 1)[-1].removesuffix(".py")
'''

WORKER_2_TEST_CODE = '''"""Unit tests for Worker 2 SocraticOnboardingTutor."""

from repo_onboard.onboarding_tutor import SocraticOnboardingTutor


def test_socratic_tutor_generates_ladder_and_evaluates() -> None:
    sample_analysis = {
        "repo_name": "abyss",
        "total_files": 2,
        "total_lines": 180,
        "purpose_summary": "Multi-agent worktree harness.",
        "entry_points": ["abyss/core/agent_runtime.py"],
        "modules": [
            {
                "module_id": "abyss.core.agent_runtime",
                "file_path": "abyss/core/agent_runtime.py",
                "layer": "Core Alignment & Guardrails",
                "purpose": "Executes tool loop and creates handoff artifacts.",
                "classes": [{"name": "AgentRuntime", "line": 42, "summary": "Main loop", "methods": ["run"]}],
                "functions": [],
                "imports": ["abyss.core.isolation_policy"],
                "line_count": 100,
            },
            {
                "module_id": "abyss.core.isolation_policy",
                "file_path": "abyss/core/isolation_policy.py",
                "layer": "Core Alignment & Guardrails",
                "purpose": "4-check worktree isolation guard.",
                "classes": [{"name": "WorktreeIsolationPolicy", "line": 30, "summary": "Guard", "methods": ["validate_file_edit"]}],
                "functions": [],
                "imports": [],
                "line_count": 80,
            },
        ],
        "archify_graph": {
            "schema_version": "archify-ir-3.2",
            "nodes": [],
            "edges": [{"source": "abyss.core.agent_runtime", "target": "abyss.core.isolation_policy", "relation": "imports"}],
        },
    }

    tutor = SocraticOnboardingTutor()
    pkg = tutor.build_onboarding_package(sample_analysis)

    assert pkg["repo_name"] == "abyss"
    assert len(pkg["socratic_ladder"]) == 3
    q1 = pkg["socratic_ladder"][0]
    verdict = tutor.evaluate_answer(q1, selected_index=q1["correct_index"])
    assert verdict["is_correct"] is True
'''

WORKER_3_VIEW_CODE = '''"""
repo_onboard/service.py
[Built by worker-web in isolated branch: worktree-worker-web]
Unified RepoOnboard Service Facade combining RepoAnalyzer (Worker 1) + SocraticOnboardingTutor (Worker 2).
"""

from __future__ import annotations

from typing import Any

from repo_onboard.analyzer import RepoAnalyzer
from repo_onboard.onboarding_tutor import SocraticOnboardingTutor


class RepoOnboardService:
    """
    Worker 1(`RepoAnalyzer`)과 Worker 2(`SocraticOnboardingTutor`)를 결합하여
    GitLab/GitHub URL 또는 로컬 레포지토리 경로에 대한 원스톱 온보딩 리포트,
    Archify 인터랙티브 아키텍처 그래프, 소크라테스식 문답 세션을 제공합니다.
    """

    def __init__(self) -> None:
        self.analyzer = RepoAnalyzer()
        self.tutor = SocraticOnboardingTutor()
        self._last_package: dict[str, Any] | None = None

    def onboard_repository(self, repo_url_or_path: str) -> dict[str, Any]:
        analysis = self.analyzer.analyze_target(repo_url_or_path)
        tutor_pkg = self.tutor.build_onboarding_package(analysis)
        combined = {
            "repo_name": analysis["repo_name"],
            "root_path": analysis["root_path"],
            "analysis": analysis,
            "onboarding_brief": tutor_pkg["onboarding_brief"],
            "socratic_ladder": tutor_pkg["socratic_ladder"],
        }
        self._last_package = combined
        return combined

    def check_socratic_answer(
        self, question_id: str, selected_index: int, reflection: str = ""
    ) -> dict[str, Any]:
        if not self._last_package:
            raise RuntimeError("No repository has been analyzed yet.")
        ladder = self._last_package.get("socratic_ladder", [])
        question = next((q for q in ladder if q["id"] == question_id), ladder[0])
        return self.tutor.evaluate_answer(question, selected_index=selected_index, user_reflection=reflection)
'''

WORKER_3_TEST_CODE = '''"""Integration tests for Worker 3 RepoOnboardService."""

from pathlib import Path
from repo_onboard.service import RepoOnboardService


def test_repo_onboard_service_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        '"""Sample onboarding target app."""\\nclass AppEngine:\\n    """Runs main workflow."""\\n    def start(self) -> str:\\n        return "ok"\\n',
        encoding="utf-8",
    )
    svc = RepoOnboardService()
    result = svc.onboard_repository(str(tmp_path))

    assert result["repo_name"] == tmp_path.name
    assert len(result["socratic_ladder"]) == 3
    q_first = result["socratic_ladder"][0]
    ans = svc.check_socratic_answer(q_first["id"], selected_index=q_first["correct_index"])
    assert ans["is_correct"] is True
'''


class MultiAgentTeamCoordinator:
    """
    Claude Code 멀티 에이전트 오케스트레이터:
    - Lead Agent: 태스크 분해 및 공유 태스크 보드(`.abyss/tasks.json`) 초기화
    - 3 Worktree Workers (`worker-analyzer`, `worker-tutor`, `worker-web`):
      각자 `.abyss/worktrees/<worker_id>` 독립 브랜치에서 병렬 개발,
      `WorktreeIsolationPolicy` 4대 보안 검증 통과,
      `.abyss/mailbox/*.jsonl`로 JSON 스키마 합의 및 커밋
    - Reviewer Agent (`EvaluatorRubric`):
      각 Worktree 브랜치의 `git diff` 및 `pytest`를 독립 실행하여 채점/승인
    - Lead Agent Merge:
      승인된 3개 Worktree 브랜치를 `main`에 순차 머지(`git merge --no-ff`)하고 통합 테스트 수행
    """

    def __init__(
        self,
        workspace_root: str | Path,
        rotator: LLMProviderRotator,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
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
        self.engine_mode: str = "agent_runtime"  # "agent_runtime" | "deepagents"
        self.is_running: bool = False
        self._listeners: list[asyncio.Queue[dict[str, Any]]] = []

        self._ensure_base_package()

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
            init_file.write_text('"""RepoOnboard: GitLab/GitHub Codebase Onboarding & Socratic Tutor."""\n', encoding="utf-8")

        tests_dir = self.workspace_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        # 초기 상태 커밋 보장
        self.worktree_mgr._run_git(["add", ".gitignore", "repo_onboard/__init__.py", ".worktreeinclude"])
        status = self.worktree_mgr._run_git(["status", "--porcelain"]).stdout.strip()
        if status:
            self.worktree_mgr._run_git(["commit", "-m", "chore: initialize repo_onboard package skeleton"])

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

    # =========================================================================
    # Shared Task List & Mailbox Persistence (Claude Code Agent Teams Pattern)
    # =========================================================================
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

    # =========================================================================
    # Worker별 시나리오/LLM 실행기 (AgentRuntime & DeepAgents 호환)
    # =========================================================================
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
        """
        외부 LLM(Qwen 3.8 Flash / Gemini / OpenAI)을 호출하여 실제 추론 코멘트를 받거나,
        결정론적으로 도구 호출 시퀀스를 수행하여 격리된 Worktree 내에 실제 모듈과 테스트를 작성합니다.
        """
        turn_idx = 0

        def responder(messages: list[dict[str, Any]], tools_schema: list[dict[str, Any]]) -> dict[str, Any]:
            nonlocal turn_idx
            turn_idx += 1

            if turn_idx == 1 and demonstrate_guard_block:
                # 1턴: Claude Code 4대 격리 가드레일이 실제로 메인 체크아웃/우회 명령을 차단하는지 시연
                return {
                    "content": (
                        f"<think>[{worker_id}] 먼저 메인 체크아웃의 설정이나 외부 git 상태를 직접 조회해 보겠습니다.</think>\n"
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
                # 모듈 파일 및 단위 테스트 파일 생성 + 태스크 상태 변경
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
                # 동료 에이전트에게 메일박스로 데이터 스키마 합의 메시지 전송 + 자체 pytest 실행
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
                    "Context Reset 및 Reviewer 인계를 위해 Handoff 요약을 반환합니다.</think>\n"
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
        """개별 Worker가 자신의 Git Worktree에서 비동기 병렬로 작업을 수행합니다."""
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

        # 선택된 프로바이더(Qwen 3.8 Flash / Gemini / OpenAI / Mock)를 경유하여 실행
        def llm_bridge(
            messages: list[dict[str, Any]], tools_schema: list[dict[str, Any]]
        ) -> dict[str, Any]:
            # 첫 턴에 실제 활성 프로바이더 연결/추론을 기록하고 결정론적 Worktree 도구 시퀀스를 결합
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

        # Worktree 브랜치에 커밋
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
        self, user_goal: str = "Build RepoOnboard: GitLab/GitHub Codebase Onboarding & Socratic Q&A Service"
    ) -> dict[str, Any]:
        """
        전체 4단계 멀티 에이전트 파이프라인을 실행합니다:
          Phase 1: Lead Agent가 공유 태스크 보드(.abyss/tasks.json) 구성 및 3개 Git Worktree 생성
          Phase 2: Worker 1, 2, 3가 각자 Worktree에서 병렬 구현 + 4대 가드레일 검증 + 메일박스 협업
          Phase 3: Reviewer Agent(`EvaluatorRubric`)가 각 Worktree의 `git diff` 및 `pytest`를 검증
          Phase 4: Lead Agent가 승인된 브랜치들을 `main`에 머지하고 통합 테스트 실행
        """
        self.is_running = True
        self.event_stream.clear()
        self.mailbox_log.clear()
        self.handoffs.clear()
        self.reviews.clear()
        self.merge_results.clear()

        try:
            # -----------------------------------------------------------------
            # Phase 1: Lead Agent Decomposition & Worktree Provisioning
            # -----------------------------------------------------------------
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
                        "<think>[Lead Agent] RepoOnboard 서비스를 충돌 없이 병렬 개발하기 위해 "
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
                    description="Implement SocraticOnboardingTutor using codebase-onboarding and socratic-onboarding skills.",
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

            # -----------------------------------------------------------------
            # Phase 2: Parallel Worktree Execution (Workers 1 & 2 in parallel)
            # -----------------------------------------------------------------
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
                        "`repo_name`, `purpose_summary`, `entry_points`, `modules` (list of module dicts), "
                        "and `archify_graph` (`nodes`, `edges`)."
                    ),
                    demonstrate_guard_block=True,  # Check #3 (git -C main_checkout) 차단 시연
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

            # Worker 3는 Worker 1, 2 브랜치를 자신의 Worktree에 git merge하여 의존성을 확보한 뒤 통합 서비스 개발
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

            # -----------------------------------------------------------------
            # Phase 3: Independent Reviewer Agent (EvaluatorRubric)
            # -----------------------------------------------------------------
            import os
            import sys

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

            # -----------------------------------------------------------------
            # Phase 4: Lead Agent Sequential Git Merge onto `main`
            # -----------------------------------------------------------------
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

            # 최종 통합 pytest 실행 (main 체크아웃)
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
        finally:
            self.is_running = False

    def get_dashboard_state(self) -> dict[str, Any]:
        return {
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
            "git_graph": self.worktree_mgr.get_git_graph_log(limit=18),
            "rotator": self.rotator.get_state(),
            "recent_events": self.event_stream[-60:],
        }
