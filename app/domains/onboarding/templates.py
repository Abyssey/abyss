"""
app/domains/onboarding/templates.py
In-process Modern DDD Repository AST Analyzer & 3-Stage Socratic Onboarding Tutor (Pure Stdlib).

Works identically on:
  - Local development folders (both Git repositories AND plain non-git project folders)
  - Cloned internal GitLab repositories and local bare remotes
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any


class RepoAnalyzer:
    """
    Python 프로젝트 폴더를 AST로 스캔하여:
    1) 프로젝트 목적 및 아키텍처 요약
    2) 모듈/클래스/함수 목록 및 핵심 진입점(Entry Points)
    3) Archify 호환 인터랙티브 아키텍처 그래프 JSON IR (nodes, edges, layers)
    4) Mermaid 아키텍처 다이어그램
    을 생성합니다.
    """

    IGNORE_DIRS = frozenset(
        {
            ".git",
            ".abyss",
            ".agents",
            "demo_workspace",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            "dist",
            "build",
        }
    )

    def analyze_directory(
        self,
        root_path: str | Path,
        *,
        repo_name: str | None = None,
        source_type: str = "local_path",
        display_source: str | None = None,
        is_git_repo: bool | None = None,
    ) -> dict[str, Any]:
        resolved_root = Path(root_path).resolve()
        if not resolved_root.exists() or not resolved_root.is_dir():
            raise ValueError(f"Repository path does not exist or is not a directory: {root_path}")

        effective_repo_name = repo_name or resolved_root.name
        effective_is_git = (
            is_git_repo if is_git_repo is not None else (resolved_root / ".git").exists()
        )

        py_files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(resolved_root):
            dirnames[:] = sorted(d for d in dirnames if d not in self.IGNORE_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    py_files.append(Path(dirpath) / fn)

        modules: list[dict[str, Any]] = []
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        known_module_ids: set[str] = set()

        for py_file in py_files[:60]:
            rel_path = py_file.relative_to(resolved_root).as_posix()
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
            first_doc_line = self._extract_summary_line(doc, rel_path)

            classes: list[dict[str, Any]] = []
            functions: list[dict[str, Any]] = []
            imports: list[str] = []

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    cls_doc = (ast.get_docstring(node) or "").strip()
                    methods = [
                        n.name
                        for n in node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ]
                    classes.append(
                        {
                            "name": node.name,
                            "line": node.lineno,
                            "summary": self._extract_summary_line(cls_doc, f"Class {node.name}"),
                            "methods": methods[:8],
                        }
                    )
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_doc = (ast.get_docstring(node) or "").strip()
                    functions.append(
                        {
                            "name": node.name,
                            "line": node.lineno,
                            "summary": self._extract_summary_line(fn_doc, f"Function {node.name}"),
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
                    if imp == target_id or imp.startswith(target_id + ".") or target_id.endswith("." + imp):
                        if src_id != target_id:
                            edge = {"source": src_id, "target": target_id, "relation": "imports"}
                            if edge not in edges:
                                edges.append(edge)

        mermaid_lines = ["flowchart LR"]
        for n in nodes[:20]:
            safe_id = n["id"].replace(".", "_").replace("-", "_")
            mermaid_lines.append(f'    {safe_id}["{n["file_path"]}"]')
        for e in edges[:32]:
            s_id = e["source"].replace(".", "_").replace("-", "_")
            t_id = e["target"].replace(".", "_").replace("-", "_")
            mermaid_lines.append(f"    {s_id} --> {t_id}")

        entry_points = [
            m["file_path"]
            for m in modules
            if any(
                k in m["file_path"]
                for k in ("app/main.py", "main.py", "server.py", "router.py", "runtime.py", "coordinator.py", "service.py")
            )
            and not m["file_path"].startswith("tests/")
        ][:5]
        if not entry_points and modules:
            entry_points = [m["file_path"] for m in modules[:3]]

        return {
            "repo_name": effective_repo_name,
            "root_path": str(resolved_root),
            "source_type": source_type,
            "display_source": display_source or str(resolved_root),
            "source_display": display_source or str(resolved_root),
            "is_git_repo": effective_is_git,
            "total_files": len(modules),
            "total_lines": sum(m["line_count"] for m in modules),
            "purpose_summary": self._synthesize_repo_purpose(effective_repo_name, modules),
            "entry_points": entry_points,
            "modules": modules,
            "archify_graph": {
                "schema_version": "archify-ir-3.2",
                "nodes": nodes,
                "edges": edges,
            },
            "mermaid": "\n".join(mermaid_lines),
        }

    @staticmethod
    def _extract_summary_line(doc: str, fallback: str) -> str:
        for raw in doc.splitlines():
            stripped = raw.strip()
            if stripped and not set(stripped) <= {"=", "-", "*"}:
                return stripped
        return fallback

    @staticmethod
    def _infer_layer(rel_path: str) -> str:
        low = rel_path.lower().replace("\\", "/")
        name = Path(low).name

        # 1. Tests first
        if low.startswith("tests/") or "/tests/" in low or name.startswith("test_"):
            return "Verification & Tests"

        # 2. Modern DDD Bounded Contexts (explicit ordering prevents "domain" substring collisions)
        if "domains/guardrails/" in low or name in ("isolation_policy.py", "permissions.py"):
            return "★ Focus Domain #1 · Guardrails (Pure Stdlib)"
        if "domains/execution/" in low or name in ("code_mode.py", "runtime.py"):
            return "★ Focus Domain #2 · Execution (Code Mode REPL Kick)"
        if "domains/workflow/" in low or name in ("coordinator.py", "worktree_adapter.py", "evaluator.py", "task_service.py"):
            return "Workflow Domain (Build, Review & Worktree Pipeline)"
        if "domains/onboarding/" in low or "repo_onboard/" in low or name in ("analyzer.py", "onboarding_tutor.py"):
            return "Onboarding Domain (Internal Git & Socratic Analysis)"

        # 3. Composition Root & Shared Kernel
        if low in ("app/main.py", "main.py") or name in ("main.py", "server.py"):
            return "Composition Root (FastAPI Entrypoint)"
        if low.startswith("app/core/") or "/core/" in low or name in ("provider_rotator.py", "git_source.py", "container.py", "config.py"):
            return "Shared Kernel (Config, DI, Multi-LLM & Internal Git)"

        # 4. Generic fallback for arbitrary external / local Python projects
        if "router" in name or "/api/" in low or low.startswith("api/"):
            return "API & Presentation Layer"
        if "service" in name or "/services/" in low or "/application/" in low:
            return "Application Services"
        if any(k in low for k in ("adapter", "client", "infra", "repository", "db/")):
            return "Adapters & Infrastructure"
        if any(k in low for k in ("model", "schema", "entity", "policy")):
            return "Domain & Models"
        return "Application Modules"

    @staticmethod
    def _synthesize_repo_purpose(repo_name: str, modules: list[dict[str, Any]]) -> str:
        layers = sorted({m["layer"] for m in modules})
        top_classes = [c["name"] for m in modules for c in m["classes"]][:6]
        return (
            f"'{repo_name}' 저장소는 {len(modules)}개의 파이썬 모듈과 {len(layers)}개 아키텍처 계층"
            f"({', '.join(layers)})으로 구성된 시스템입니다. "
            f"핵심 컴포넌트({', '.join(top_classes) or 'Core Modules'})들이 명확한 경계로 연결되어 동작합니다."
        )


class SocraticOnboardingTutor:
    """
    `RepoAnalyzer`가 생성한 AST/아키텍처 분석 결과를 입력받아:
    1) 신규 합류 개발자를 위한 온보딩 브리핑 (Executive Summary, Reading Order, Layer Breakdown)
    2) 3단계 소크라테스 문답 사다리 (Entry Point → ★ Focus Domain Safety → Cross-Module Dependency)
    를 생성하고 답변을 채점합니다.
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

        # Stage 1: 진입점과 제어 흐름
        first_entry = entry_points[0] if entry_points else (modules[0]["file_path"] if modules else "main.py")
        entry_mod = next((m for m in modules if m["file_path"] == first_entry), modules[0] if modules else {})
        entry_symbols = [c["name"] for c in entry_mod.get("classes", [])] + [
            f["name"] for f in entry_mod.get("functions", [])
        ]
        sym_label = entry_symbols[0] if entry_symbols else _path_stem(first_entry)

        questions.append(
            {
                "id": "stage-1-entrypoint",
                "stage": "Stage 1 · Entry Point & Control Flow",
                "question": (
                    f"이 프로젝트({repo_name})에서 요청이나 작업이 시작될 때 가장 먼저 살펴봐야 할 "
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
                    f"`{first_entry}` 모듈이 `{sym_label}` 등을 통해 핵심 흐름을 시작하고 하위 도메인을 호출합니다."
                ),
                "source_evidence": f"{first_entry}:1",
                "follow_up_prompt": f"만약 `{first_entry}`에 새로운 기능을 추가한다면 어느 도메인과 먼저 연결해야 할까요?",
            }
        )

        # Stage 2: ★ Focus Domain 우선 선택 → 핵심 도메인 불변성/안전 경계
        focus_mods = [m for m in modules if m["layer"].startswith("★ Focus Domain") and m.get("classes")]
        if not focus_mods:
            focus_mods = [m for m in modules if m["layer"].startswith("★ Focus Domain")]
        if not focus_mods:
            focus_mods = [
                m
                for m in modules
                if ("Domain" in m["layer"] or "policy" in m["file_path"] or len(m.get("classes", [])) > 0)
                and not m["file_path"].startswith("tests/")
            ]
        target_core = focus_mods[0] if focus_mods else (modules[-1] if modules else {})
        core_file = target_core.get("file_path", first_entry)
        core_cls = (
            target_core.get("classes", [{}])[0].get("name", "CoreComponent")
            if target_core.get("classes")
            else _path_stem(core_file)
        )
        core_line = (
            target_core.get("classes", [{}])[0].get("line", 1)
            if target_core.get("classes")
            else 1
        )

        questions.append(
            {
                "id": "stage-2-invariant",
                "stage": "Stage 2 · Focus Domain Invariants & Safety Boundary",
                "question": (
                    f"`{core_file}`에 정의된 `{core_cls}`는 시스템에서 어떤 결정론적 보장(Invariant)이나 "
                    f"핵심 도메인 규칙을 책임지고 있나요?"
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
                    "시스템의 안정성과 경계를 보장합니다."
                ),
                "source_evidence": f"{core_file}:{core_line}",
                "follow_up_prompt": "이 검증 로직이 우회되거나 제거된다면 어떤 장애가 발생할 수 있을지 생각해 보세요.",
            }
        )

        # Stage 3: 모듈 간 의존성 및 데이터 계약
        if edges:
            sample_edge = edges[0]
            src_mod = sample_edge["source"]
            dst_mod = sample_edge["target"]
        else:
            src_mod = modules[0]["module_id"] if modules else "app.main"
            dst_mod = modules[-1]["module_id"] if len(modules) > 1 else "app.domains.guardrails"

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
                    f"`{src_mod}`는 `{dst_mod}`의 책임 영역을 중복 구현하지 않고, 명확한 데이터 계약을 통해 결합도를 낮춥니다."
                ),
                "source_evidence": f"{src_mod} -> {dst_mod}",
                "follow_up_prompt": "두 모듈 사이의 반환 스키마가 변경될 때 깨지지 않으려면 어떤 테스트가 필요할까요?",
            }
        )

        return questions


def _path_stem(file_path: str) -> str:
    return file_path.rsplit("/", 1)[-1].removesuffix(".py")
