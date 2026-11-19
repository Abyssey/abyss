"""
app/domains/workflow/task_service.py
Act 2 Visual App Build + Live Preview & Act 4 Independent Read-Only Code/MR Review Service.

Architectural lineage:
  - Act 2 Build + Live Preview (`open-swe` `openwiki` + `docs/PLUGINS.md` preview pattern):
    Default `project_map_app` generates a genuine Python AST repository scanner (`project_map/builder.py`)
    and executes it inside an isolated Git worktree to produce a 100% self-contained interactive HTML5
    web app (`project_map/index.html`) — zero hardcoded dummy data, zero external CDN/font links,
    zero `localStorage`/`window.parent` calls for safe `<iframe sandbox="allow-scripts">` rendering.
  - Secondary `inspection_dashboard`:
    Generates a genuine Python service/workspace health checker (`inspection_dashboard/checker.py`)
    and executes it to produce a standalone interactive Operations Inspection Dashboard HTML5 app.
  - Primary Mode #1 (Local Dev Folder) safety:
    Works cleanly on BOTH local Git repositories (`self`, `demo`, `/path/to/repo`) AND plain non-git
    local folders (`/path/to/plain-folder`) without EVER mutating the user's original directory or `main`!
  - Act 4 Independent Read-Only Review (`open-swe` Reviewer + `walkthrough` guided diff review):
    Strictly read-only (never edits files or creates commits on the target branch), returning
    beginner-friendly Korean `file:line` findings with concrete fix suggestions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from app.core.config import AppSettings, load_settings
from app.core.git_source import InternalGitSourceResolver, sanitize_git_text
from app.core.llm.provider_rotator import LLMProviderRotator
from app.domains.execution.code_mode import CodeModeExecutor
from app.domains.execution.runtime import AgentRuntime
from app.domains.guardrails.isolation_policy import WorktreeIsolationPolicy
from app.domains.guardrails.permissions import PermissionRules
from app.domains.workflow.evaluator import EvaluatorRubric
from app.domains.workflow.worktree_adapter import GitWorktreeManager
from app.domains.workflow.worktree_tools import (
    _SENSITIVE_ENV_KEY_RE,
    build_agent_tools,
    build_safe_subprocess_env,
)

_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,32}$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9_./@^~:{}-]+$")
_COMMIT_RANGE_RE = re.compile(r"^[A-Za-z0-9_./@^~:{}-]+(?:\.\.\.?[A-Za-z0-9_./@^~:{}-]+)?$")
_SNAPSHOT_MAX_FILES = 2000
_SNAPSHOT_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB per file
_SNAPSHOT_IGNORE_DIRS = frozenset(
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


def _b64_arg(value: str) -> str:
    """Encode arbitrary user text as URL-safe ASCII base64 (zero shell metacharacters)."""
    return base64.urlsafe_b64encode((value or "").encode("utf-8")).decode("ascii")


def _build_safe_subprocess_env(pythonpath: str | Path) -> dict[str, str]:
    """Return a scrubbed subprocess environment with Pod secrets and Git auth config stripped."""
    return build_safe_subprocess_env(pythonpath=pythonpath)


def _validate_git_ref(ref: str, label: str = "git ref") -> str:
    if any(ch in ref for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"Invalid {label}: control characters are not allowed.")
    cleaned = ref.strip()
    if (
        not cleaned
        or cleaned.startswith("-")
        or ".." in cleaned
        or not _GIT_REF_RE.fullmatch(cleaned)
    ):
        raise ValueError(f"Invalid {label}: {cleaned!r}")
    return cleaned


def _validate_commit_range(commit_range: str) -> str:
    if any(ch in commit_range for ch in ("\r", "\n", "\x00")):
        raise ValueError("Invalid commit_range: control characters are not allowed.")
    cleaned = commit_range.strip()
    if not cleaned or cleaned.startswith("-") or not _COMMIT_RANGE_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid commit_range: {cleaned!r}")
    if "..." in cleaned:
        parts = cleaned.split("...", 1)
    elif ".." in cleaned:
        parts = cleaned.split("..", 1)
    else:
        parts = [cleaned]
    for part in parts:
        _validate_git_ref(part, label="commit_range endpoint")
    return cleaned


# =============================================================================
# Act 4 Preset 4-A: Realistic Defective Dashboard MR Diff (REQUEST_CHANGES)
# =============================================================================
DEFECTIVE_MR_SAMPLE_DIFF = """diff --git a/inspection_dashboard/api_handler.py b/inspection_dashboard/api_handler.py
new file mode 100644
--- /dev/null
+++ b/inspection_dashboard/api_handler.py
@@ -0,0 +1,22 @@
+\"\"\"Quick API handler for pushing live inspection metrics to internal monitor.\"\"\"
+
+import json
+
+API_SECRET = "sk-prod-live-9876543210-internal"
+
+
+def fetch_service_payload(raw_body: str) -> dict:
+    try:
+        parsed = json.loads(raw_body)
+    except:
+        pass
+    return {"status": "ok", "token_loaded": bool(API_SECRET)}
+
+
+def evaluate_filter_expression(expr: str) -> bool:
+    try:
+        return bool(expr)
+    except Exception:
+        pass
+    return False
"""


# =============================================================================
# Act 2 Preset #1 (DEFAULT): `project_map_app` Genuine AST Scanner + Interactive HTML App
# =============================================================================
PROJECT_MAP_INIT_CODE = '''"""Interactive Project Structure Map generator (AST scanner + standalone HTML5 app)."""

from project_map.builder import build_project_map_html, generate_project_map, scan_repository

__all__ = ["build_project_map_html", "generate_project_map", "scan_repository"]
'''

PROJECT_MAP_BUILDER_CODE = r'''"""
project_map/builder.py
Genuine Python AST Repository Scanner & Standalone Interactive HTML5 Project Map Generator.

Scans real `.py` source files on disk (zero dummy data) and compiles them into a single-file
interactive web app (`project_map/index.html`) safe for `<iframe sandbox="allow-scripts">`
and air-gapped internal networks (zero external CDN/fonts, zero localStorage/window.parent).
"""

from __future__ import annotations

import argparse
import ast
import base64
import html
import json
import os
from pathlib import Path
from typing import Any

IGNORE_DIRS = frozenset(
    {
        ".git",
        ".abyss",
        ".agents",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "demo_workspace",
        "dist",
        "build",
    }
)


def _infer_domain(rel_path: str) -> str:
    low = rel_path.lower().replace("\\", "/")
    name = Path(low).name
    if low.startswith("tests/") or "/tests/" in low or name.startswith("test_"):
        return "Verification & Tests"
    if "domains/guardrails/" in low or name in ("isolation_policy.py", "permissions.py"):
        return "★ Focus Domain #1 · Guardrails"
    if "domains/execution/" in low or name in ("code_mode.py", "runtime.py"):
        return "★ Focus Domain #2 · Execution (REPL)"
    if "domains/workflow/" in low or name in ("coordinator.py", "worktree_adapter.py", "evaluator.py", "task_service.py"):
        return "Workflow Domain"
    if "domains/onboarding/" in low or "repo_onboard/" in low:
        return "Onboarding Domain"
    if low in ("app/main.py", "main.py") or name in ("main.py", "server.py"):
        return "Composition Root"
    if low.startswith("app/core/") or "/core/" in low:
        return "Shared Kernel"
    parts = [p for p in rel_path.split("/") if p]
    if len(parts) > 1:
        return f"Package · {parts[0]}"
    return "Root Modules"


def _first_doc_line(doc: str, fallback: str) -> str:
    for line in doc.splitlines():
        s = line.strip()
        if s and not set(s) <= {"=", "-", "*"}:
            return s
    return fallback


def scan_repository(root_path: str | Path, *, repo_name: str | None = None) -> dict[str, Any]:
    """Scan all real Python files under `root_path` via `ast` and build module/dependency IR."""
    root = Path(root_path).resolve()
    py_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                py_files.append(Path(dirpath) / fn)

    modules: list[dict[str, Any]] = []
    known_ids: set[str] = set()

    for py_file in py_files[:80]:
        rel_path = py_file.relative_to(root).as_posix()
        mod_id = rel_path.removesuffix(".py").replace("/", ".")
        if mod_id.endswith(".__init__"):
            mod_id = mod_id.removesuffix(".__init__")
        if not mod_id:
            continue
        known_ids.add(mod_id)

        source = py_file.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        doc = (ast.get_docstring(tree) or "").strip()
        summary = _first_doc_line(doc, f"Module {rel_path}")
        classes: list[dict[str, Any]] = []
        functions: list[dict[str, Any]] = []
        imports: list[str] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                methods = [
                    n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                classes.append(
                    {
                        "name": node.name,
                        "line": node.lineno,
                        "methods": methods[:10],
                        "doc": _first_doc_line((ast.get_docstring(node) or "").strip(), f"Class {node.name}"),
                    }
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(
                    {
                        "name": node.name,
                        "line": node.lineno,
                        "doc": _first_doc_line((ast.get_docstring(node) or "").strip(), f"Function {node.name}"),
                    }
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)

        modules.append(
            {
                "id": mod_id,
                "file_path": rel_path,
                "name": Path(rel_path).name,
                "domain": _infer_domain(rel_path),
                "summary": summary[:160],
                "classes": classes,
                "functions": functions,
                "imports": imports,
                "line_count": len(source.splitlines()),
            }
        )

    edges: list[dict[str, str]] = []
    for mod in modules:
        src = mod["id"]
        for imp in mod["imports"]:
            for target in known_ids:
                if target != src and (imp == target or imp.startswith(target + ".") or target.endswith("." + imp)):
                    edge = {"source": src, "target": target}
                    if edge not in edges:
                        edges.append(edge)

    domain_counts: dict[str, int] = {}
    for mod in modules:
        domain_counts[mod["domain"]] = domain_counts.get(mod["domain"], 0) + 1

    entry_points = [
        m["file_path"]
        for m in modules
        if any(k in m["file_path"] for k in ("main.py", "service.py", "builder.py", "runtime.py", "coordinator.py"))
        and not m["file_path"].startswith("tests/")
    ][:4]
    if not entry_points and modules:
        entry_points = [m["file_path"] for m in modules[:3]]

    test_files = [m["file_path"] for m in modules if m["domain"] == "Verification & Tests"]
    focus_files = [m["file_path"] for m in modules if m["domain"].startswith("★ Focus Domain")]

    checklist: list[dict[str, str]] = []
    if entry_points:
        checklist.append(
            {
                "id": "chk-entry",
                "title": f"진입점 파일 파악하기: {entry_points[0]}",
                "detail": "요청과 작업 흐름이 어디서 시작되는지 상단 함수/클래스를 먼저 확인하세요.",
            }
        )
    if focus_files:
        checklist.append(
            {
                "id": "chk-focus",
                "title": f"핵심 도메인 확인하기: {focus_files[0]}",
                "detail": "외부 의존성 없이 안전 규칙과 실행 로직을 담당하는 핵심 모듈을 살펴보세요.",
            }
        )
    elif len(modules) > 1:
        checklist.append(
            {
                "id": "chk-core",
                "title": f"주요 모듈 살펴보기: {modules[0]['file_path']}",
                "detail": "가장 많은 클래스/함수가 정의된 핵심 파일의 역할을 확인하세요.",
            }
        )
    if edges:
        checklist.append(
            {
                "id": "chk-deps",
                "title": f"모듈 의존성 추적하기 ({edges[0]['source']} → {edges[0]['target']})",
                "detail": "상위 서비스가 하위 모듈을 어떤 데이터 계약으로 호출하는지 클릭해 확인하세요.",
            }
        )
    if test_files:
        checklist.append(
            {
                "id": "chk-tests",
                "title": f"자동화 테스트 실행하기: pytest {test_files[0]}",
                "detail": "테스트 코드를 읽으면 각 모듈이 보장하는 정상/예외 동작을 가장 빨리 이해할 수 있습니다.",
            }
        )

    return {
        "repo_name": (repo_name or root.name).strip() or root.name,
        "total_files": len(modules),
        "total_lines": sum(m["line_count"] for m in modules),
        "domains": [{"name": k, "count": v} for k, v in sorted(domain_counts.items())],
        "entry_points": entry_points,
        "modules": modules,
        "edges": edges,
        "onboarding_checklist": checklist,
    }


def build_project_map_html(scan_data: dict[str, Any], instruction: str = "") -> str:
    """Render a 100% self-contained interactive HTML5 Project Structure Map web app."""
    safe_json = json.dumps(scan_data, ensure_ascii=False).replace("</", "<\\/")
    repo_name = html.escape(str(scan_data.get("repo_name", "Project")))
    subtitle = html.escape(
        instruction.strip() or "AST로 실제 소스 파일을 스캔해 생성한 인터랙티브 프로젝트 구조 지도"
    )
    total_files = int(scan_data.get("total_files", 0))
    total_lines = int(scan_data.get("total_lines", 0))
    total_domains = len(scan_data.get("domains", []))
    total_edges = len(scan_data.get("edges", []))

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{repo_name} — Interactive Project Map</title>
  <style>
    :root {{
      --bg: #0b1020; --surface: #121a30; --card: #18233f; --border: #2a3a63;
      --text: #e8eefc; --muted: #9fb0d6; --accent: #38bdf8; --gold: #f59e0b;
      --green: #34d399; --chip: #1e2c50;
    }}
    body.light {{
      --bg: #f4f7fb; --surface: #ffffff; --card: #ffffff; --border: #d7e0ef;
      --text: #111827; --muted: #4b5563; --accent: #0284c7; --gold: #d97706;
      --green: #059669; --chip: #e8f0fe;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.5;
    }}
    .wrap {{ max-width: 1180px; margin: 0 auto; padding: 18px 20px 32px; }}
    header {{
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px;
      padding: 16px 20px; background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
    }}
    h1 {{ margin: 0; font-size: 1.35rem; }}
    .sub {{ margin: 4px 0 0; color: var(--muted); font-size: 0.88rem; }}
    .btn {{
      background: var(--chip); color: var(--text); border: 1px solid var(--border);
      padding: 7px 12px; border-radius: 9px; cursor: pointer; font-size: 0.84rem; font-weight: 600;
    }}
    .btn:hover {{ border-color: var(--accent); }}
    .kpis {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 14px 0;
    }}
    .kpi {{
      background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px;
    }}
    .kpi .num {{ font-size: 1.45rem; font-weight: 700; color: var(--accent); }}
    .kpi .lbl {{ font-size: 0.78rem; color: var(--muted); }}
    .toolbar {{
      display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 14px;
    }}
    #search-input {{
      flex: 1 1 240px; padding: 9px 13px; border-radius: 10px; border: 1px solid var(--border);
      background: var(--surface); color: var(--text); font-size: 0.9rem;
    }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .chip {{
      padding: 6px 11px; border-radius: 999px; border: 1px solid var(--border);
      background: var(--chip); color: var(--text); font-size: 0.78rem; cursor: pointer;
    }}
    .chip.active {{ background: var(--accent); color: #04131f; border-color: var(--accent); font-weight: 700; }}
    .chip.gold {{ border-color: var(--gold); color: var(--gold); }}
    .chip.gold.active {{ background: var(--gold); color: #1b1200; }}
    .layout {{ display: grid; grid-template-columns: 1.65fr 1fr; gap: 16px; align-items: start; }}
    @media (max-width: 860px) {{ .layout {{ grid-template-columns: 1fr; }} }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }}
    .card {{
      background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px;
      cursor: pointer; transition: transform .12s ease, border-color .12s ease;
    }}
    .card:hover {{ transform: translateY(-2px); border-color: var(--accent); }}
    .card.selected {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(56,189,248,.25); }}
    .card.focus {{ border-left: 4px solid var(--gold); }}
    .badge {{
      display: inline-block; font-size: 0.7rem; padding: 2px 8px; border-radius: 999px;
      background: var(--chip); color: var(--accent); margin-bottom: 6px; font-weight: 600;
    }}
    .card.focus .badge {{ color: var(--gold); }}
    .fpath {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.84rem; font-weight: 700; word-break: break-all; }}
    .summary {{ font-size: 0.8rem; color: var(--muted); margin: 6px 0; }}
    .meta {{ font-size: 0.74rem; color: var(--muted); display: flex; justify-content: space-between; }}
    .panel {{
      background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px;
      margin-bottom: 14px;
    }}
    .panel h2 {{ margin: 0 0 10px; font-size: 1rem; }}
    .pill-list {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }}
    .dep-btn {{
      font-family: ui-monospace, Menlo, monospace; font-size: 0.76rem; padding: 4px 8px;
      border-radius: 7px; border: 1px solid var(--border); background: var(--chip); color: var(--accent); cursor: pointer;
    }}
    .check-item {{
      display: flex; gap: 9px; align-items: flex-start; padding: 8px 0; border-bottom: 1px solid var(--border);
      cursor: pointer; font-size: 0.84rem;
    }}
    .check-item:last-child {{ border-bottom: none; }}
    .progress-bar {{ height: 8px; background: var(--chip); border-radius: 999px; overflow: hidden; margin: 6px 0 10px; }}
    .progress-fill {{ height: 100%; background: var(--green); width: 0%; transition: width .2s ease; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>🗺️ {repo_name} — Interactive Project Map</h1>
        <p class="sub">{subtitle}</p>
      </div>
      <button id="theme-toggle" class="btn" type="button">🌓 테마 전환</button>
    </header>

    <section class="kpis">
      <div class="kpi"><div class="num">{total_files}</div><div class="lbl">스캔된 Python 파일</div></div>
      <div class="kpi"><div class="num">{total_lines}</div><div class="lbl">총 코드 라인</div></div>
      <div class="kpi"><div class="num">{total_domains}</div><div class="lbl">도메인 / 레이어</div></div>
      <div class="kpi"><div class="num">{total_edges}</div><div class="lbl">내부 모듈 의존성</div></div>
    </section>

    <section class="toolbar">
      <input id="search-input" type="search" placeholder="파일명, 클래스, 함수, 역할 키워드 검색..." />
      <div id="domain-chips" class="chips"></div>
    </section>

    <div class="layout">
      <main>
        <div id="module-grid" class="cards"></div>
      </main>
      <aside>
        <div id="detail-drawer" class="panel">
          <h2>📄 파일 상세 인스펙터</h2>
          <p class="sub">왼쪽 카드를 클릭하면 클래스·함수·연결된 모듈을 바로 볼 수 있습니다.</p>
        </div>
        <div id="onboarding-checklist" class="panel">
          <h2>✅ 입문자 온보딩 체크리스트</h2>
          <div class="sub"><span id="chk-progress-text">0% 완료</span></div>
          <div class="progress-bar"><div id="chk-progress-fill" class="progress-fill"></div></div>
          <div id="chk-list"></div>
        </div>
      </aside>
    </div>
  </div>

  <script id="project-map-data" type="application/json">{safe_json}</script>
  <script>
    (function () {{
      const data = JSON.parse(document.getElementById("project-map-data").textContent || "{{}}");
      const modules = data.modules || [];
      const edges = data.edges || [];
      const checklist = data.onboarding_checklist || [];
      let activeDomain = "ALL";
      let query = "";
      let selectedId = modules.length ? modules[0].id : null;
      const checkedState = {{}};

      const chipsEl = document.getElementById("domain-chips");
      const gridEl = document.getElementById("module-grid");
      const drawerEl = document.getElementById("detail-drawer");
      const searchEl = document.getElementById("search-input");
      const chkListEl = document.getElementById("chk-list");
      const chkFillEl = document.getElementById("chk-progress-fill");
      const chkTextEl = document.getElementById("chk-progress-text");

      function esc(str) {{
        return String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
      }}

      function renderChips() {{
        const domains = [{{"name": "ALL", "count": modules.length}}].concat(data.domains || []);
        chipsEl.innerHTML = domains.map((d) => {{
          const isFocus = d.name.indexOf("★") === 0;
          const cls = "chip" + (isFocus ? " gold" : "") + (activeDomain === d.name ? " active" : "");
          const label = d.name === "ALL" ? "전체 (" + d.count + ")" : d.name + " (" + d.count + ")";
          return '<button type="button" class="' + cls + '" data-domain="' + esc(d.name) + '">' + esc(label) + "</button>";
        }}).join("");
        chipsEl.querySelectorAll("button").forEach((btn) => {{
          btn.addEventListener("click", () => {{
            activeDomain = btn.getAttribute("data-domain") || "ALL";
            renderChips();
            renderGrid();
          }});
        }});
      }}

      function matchesQuery(m) {{
        if (activeDomain !== "ALL" && m.domain !== activeDomain) return false;
        if (!query) return true;
        const q = query.toLowerCase();
        const hay = [
          m.file_path, m.domain, m.summary,
          ...(m.classes || []).map((c) => c.name + " " + (c.methods || []).join(" ")),
          ...(m.functions || []).map((f) => f.name)
        ].join(" ").toLowerCase();
        return hay.includes(q);
      }}

      function renderGrid() {{
        const filtered = modules.filter(matchesQuery);
        if (!filtered.length) {{
          gridEl.innerHTML = '<div class="panel">검색 조건에 맞는 모듈이 없습니다.</div>';
          return;
        }}
        gridEl.innerHTML = filtered.map((m) => {{
          const isFocus = m.domain.indexOf("★") === 0;
          const cls = "card" + (isFocus ? " focus" : "") + (m.id === selectedId ? " selected" : "");
          return (
            '<div class="' + cls + '" data-id="' + esc(m.id) + '">' +
              '<div class="badge">' + esc(m.domain) + '</div>' +
              '<div class="fpath">' + esc(m.file_path) + '</div>' +
              '<div class="summary">' + esc(m.summary) + '</div>' +
              '<div class="meta"><span>클래스 ' + (m.classes || []).length + ' · 함수 ' + (m.functions || []).length + '</span><span>' + m.line_count + ' lines</span></div>' +
            '</div>'
          );
        }}).join("");
        gridEl.querySelectorAll(".card").forEach((card) => {{
          card.addEventListener("click", () => {{
            selectedId = card.getAttribute("data-id");
            renderGrid();
            renderDrawer();
          }});
        }});
      }}

      function renderDrawer() {{
        const m = modules.find((x) => x.id === selectedId);
        if (!m) return;
        const outgoing = edges.filter((e) => e.source === m.id).map((e) => e.target);
        const incoming = edges.filter((e) => e.target === m.id).map((e) => e.source);
        const clsHtml = (m.classes || []).length
          ? (m.classes || []).map((c) => '<li><strong>' + esc(c.name) + '</strong> (L' + c.line + ') — ' + esc((c.methods || []).join(", ") || "no methods") + '</li>').join("")
          : "<li>클래스 없음</li>";
        const fnHtml = (m.functions || []).length
          ? (m.functions || []).map((f) => '<li><strong>' + esc(f.name) + '()</strong> (L' + f.line + ')</li>').join("")
          : "<li>최상위 함수 없음</li>";
        const outHtml = outgoing.length
          ? outgoing.map((id) => '<button type="button" class="dep-btn" data-jump="' + esc(id) + '">→ ' + esc(id) + '</button>').join("")
          : '<span class="sub">내부 호출 없음 (독립 순수 모듈)</span>';
        const inHtml = incoming.length
          ? incoming.map((id) => '<button type="button" class="dep-btn" data-jump="' + esc(id) + '">← ' + esc(id) + '</button>').join("")
          : '<span class="sub">상위 호출자 없음 (진입점)</span>';

        drawerEl.innerHTML =
          '<h2>📄 ' + esc(m.file_path) + '</h2>' +
          '<div class="badge">' + esc(m.domain) + '</div>' +
          '<p class="summary">' + esc(m.summary) + '</p>' +
          '<strong>주요 클래스</strong><ul style="margin:4px 0 10px;padding-left:18px;font-size:0.82rem;">' + clsHtml + '</ul>' +
          '<strong>주요 함수</strong><ul style="margin:4px 0 10px;padding-left:18px;font-size:0.82rem;">' + fnHtml + '</ul>' +
          '<strong>이 파일이 사용하는 모듈</strong><div class="pill-list">' + outHtml + '</div>' +
          '<div style="height:8px"></div>' +
          '<strong>이 파일을 사용하는 모듈</strong><div class="pill-list">' + inHtml + '</div>';

        drawerEl.querySelectorAll("[data-jump]").forEach((btn) => {{
          btn.addEventListener("click", () => {{
            selectedId = btn.getAttribute("data-jump");
            activeDomain = "ALL";
            renderChips();
            renderGrid();
            renderDrawer();
          }});
        }});
      }}

      function renderChecklist() {{
        if (!checklist.length) {{
          chkListEl.innerHTML = '<p class="sub">체크리스트 항목이 없습니다.</p>';
          return;
        }}
        chkListEl.innerHTML = checklist.map((item) => {{
          const checked = checkedState[item.id] ? "checked" : "";
          return (
            '<label class="check-item">' +
              '<input type="checkbox" data-chk="' + esc(item.id) + '" ' + checked + ' />' +
              '<div><strong>' + esc(item.title) + '</strong><div class="sub">' + esc(item.detail) + '</div></div>' +
            '</label>'
          );
        }}).join("");
        const done = checklist.filter((i) => checkedState[i.id]).length;
        const pct = Math.round((done / checklist.length) * 100);
        chkFillEl.style.width = pct + "%";
        chkTextEl.textContent = done + " / " + checklist.length + " 완료 (" + pct + "%)";
        chkListEl.querySelectorAll("input[type=checkbox]").forEach((cb) => {{
          cb.addEventListener("change", () => {{
            checkedState[cb.getAttribute("data-chk")] = cb.checked;
            renderChecklist();
          }});
        }});
      }}

      searchEl.addEventListener("input", () => {{
        query = searchEl.value.trim();
        renderGrid();
      }});

      document.getElementById("theme-toggle").addEventListener("click", () => {{
        document.body.classList.toggle("light");
      }});

      renderChips();
      renderGrid();
      renderDrawer();
      renderChecklist();
    }})();
  </script>
</body>
</html>
"""


def generate_project_map(
    root_path: str | Path,
    output_html: str | Path,
    instruction: str = "",
    *,
    repo_name: str | None = None,
) -> dict[str, Any]:
    """Scan `root_path` and write the standalone interactive HTML map to `output_html`."""
    scan_data = scan_repository(root_path, repo_name=repo_name)
    rendered = build_project_map_html(scan_data, instruction=instruction)
    out = Path(output_html)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    return {
        "output_html": str(out),
        "repo_name": scan_data["repo_name"],
        "total_files": scan_data["total_files"],
        "total_lines": scan_data["total_lines"],
        "domains_count": len(scan_data["domains"]),
        "edges_count": len(scan_data["edges"]),
    }


def _decode_b64_arg(encoded: str) -> str:
    if not encoded:
        return ""
    return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate interactive project structure map HTML.")
    parser.add_argument("--root", default=".", help="Repository root to AST-scan.")
    parser.add_argument("--output", default="project_map/index.html", help="Output HTML file path.")
    parser.add_argument("--instruction", default="", help="Optional user instruction subtitle.")
    parser.add_argument("--instruction-b64", default="", help="Base64-encoded user instruction subtitle.")
    parser.add_argument("--repo-name", default="", help="Optional display repository name.")
    parser.add_argument("--repo-name-b64", default="", help="Base64-encoded display repository name.")
    args = parser.parse_args()
    instruction = _decode_b64_arg(args.instruction_b64) if args.instruction_b64 else args.instruction
    repo_name = (_decode_b64_arg(args.repo_name_b64) if args.repo_name_b64 else args.repo_name) or None
    summary = generate_project_map(
        args.root,
        args.output,
        instruction=instruction,
        repo_name=repo_name,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

PROJECT_MAP_TEST_CODE = r'''"""Unit tests for the generated `project_map` AST scanner & interactive HTML app."""

from __future__ import annotations

from pathlib import Path

from project_map.builder import build_project_map_html, generate_project_map, scan_repository


def test_scan_repository_discovers_real_python_files_on_disk(tmp_path: Path) -> None:
    sample_pkg = tmp_path / "sample_service"
    sample_pkg.mkdir()
    (sample_pkg / "policy.py").write_text(
        '"""Policy module."""\nclass SafetyGuard:\n    def check(self) -> bool:\n        return True\n',
        encoding="utf-8",
    )
    (sample_pkg / "runner.py").write_text(
        '"""Runner entrypoint."""\nfrom sample_service.policy import SafetyGuard\ndef run() -> bool:\n    return SafetyGuard().check()\n',
        encoding="utf-8",
    )

    report = scan_repository(tmp_path)
    assert report["total_files"] == 2
    assert any(m["file_path"] == "sample_service/policy.py" for m in report["modules"])
    assert any(e["source"] == "sample_service.runner" and e["target"] == "sample_service.policy" for e in report["edges"])


def test_generate_project_map_produces_self_contained_interactive_html(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out_html = tmp_path / "project_map.html"
    summary = generate_project_map(repo_root, out_html, instruction="프로젝트 구조 지도")

    assert summary["total_files"] >= 2
    content = out_html.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "project-map-data" in content
    assert "detail-drawer" in content
    assert "onboarding-checklist" in content
    assert "project_map/builder.py" in content
    # Air-gap Pod safety: zero external CDN/font URLs
    assert "fonts.googleapis.com" not in content
    assert "cdn.jsdelivr.net" not in content
    assert "localStorage" not in content
'''


# =============================================================================
# Act 2 Preset #2 (SECONDARY): `inspection_dashboard` Genuine Health Checker + Interactive HTML
# =============================================================================
INSPECTION_INIT_CODE = '''"""Operations Inspection Dashboard (real workspace health checks + standalone HTML5 UI)."""

from inspection_dashboard.checker import (
    ServiceCheck,
    generate_inspection_dashboard,
    render_dashboard_html,
    run_health_checks,
    summarize_checks,
)

__all__ = [
    "ServiceCheck",
    "generate_inspection_dashboard",
    "render_dashboard_html",
    "run_health_checks",
    "summarize_checks",
]
'''

INSPECTION_CHECKER_CODE = r'''"""
inspection_dashboard/checker.py
Genuine Workspace & Service Health Inspection Checker + Standalone Interactive HTML5 Dashboard.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

CheckStatus = Literal["healthy", "warning", "critical"]


@dataclass
class ServiceCheck:
    id: str
    name: str
    category: str
    status: CheckStatus
    latency_ms: float
    detail: str
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_health_checks(root_path: str | Path, *, repo_name: str | None = None) -> list[ServiceCheck]:
    """Run real local workspace, Git, Python, test suite, and TLS/config health checks."""
    root = Path(root_path).resolve()
    display_name = (repo_name or root.name).strip() or root.name
    checks: list[ServiceCheck] = []

    # 1. Python runtime
    t0 = time.perf_counter()
    py_ver = platform.python_version()
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        ServiceCheck(
            id="chk-python",
            name="Python Runtime Engine",
            category="Runtime",
            status="healthy" if py_ok else "warning",
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail=f"Python {py_ver} ({platform.system()} {platform.machine()})",
            remediation="" if py_ok else "Python 3.11 이상 환경을 권장합니다.",
        )
    )

    # 2. Git CLI & Repo status
    t0 = time.perf_counter()
    git_bin = shutil.which("git")
    if git_bin and (root / ".git").exists():
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        branch = proc.stdout.strip() or "HEAD"
        git_status: CheckStatus = "healthy" if proc.returncode == 0 else "warning"
        git_detail = f"Git active on branch '{branch}'"
        git_rem = ""
    elif git_bin:
        git_status = "warning"
        git_detail = "Git CLI 설치됨 (현재 디렉토리는 일반 로컬 폴더)"
        git_rem = "버전 관리가 필요하면 `git init -b main`으로 초기화하세요."
    else:
        git_status = "critical"
        git_detail = "Git CLI를 찾을 수 없습니다."
        git_rem = "컨테이너 이미지에 `git` 패키지를 설치하세요."
    checks.append(
        ServiceCheck(
            id="chk-git",
            name="Git Worktree & Branch Engine",
            category="SCM",
            status=git_status,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail=git_detail,
            remediation=git_rem,
        )
    )

    # 3. Source files inventory
    t0 = time.perf_counter()
    py_count = sum(
        1
        for p in root.rglob("*.py")
        if not (set(p.relative_to(root).parts) & {".git", ".venv", "__pycache__", ".abyss"})
    )
    checks.append(
        ServiceCheck(
            id="chk-sources",
            name="Repository Python Modules",
            category="Codebase",
            status="healthy" if py_count > 0 else "warning",
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail=f"활성 Python 모듈 {py_count}개 감지됨 ({display_name})",
            remediation="" if py_count > 0 else "대상 프로젝트에 .py 소스 파일이 없습니다.",
        )
    )

    # 4. Automated tests presence
    t0 = time.perf_counter()
    tests_dir = root / "tests"
    test_count = len(list(tests_dir.rglob("test_*.py"))) if tests_dir.exists() else 0
    checks.append(
        ServiceCheck(
            id="chk-tests",
            name="Automated Pytest Suite",
            category="Quality",
            status="healthy" if test_count > 0 else "warning",
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail=f"tests/ 하위 테스트 모듈 {test_count}개 준비됨",
            remediation="" if test_count > 0 else "tests/test_*.py 단위 테스트를 추가하세요.",
        )
    )

    # 5. Internal GitLab / TLS Readiness
    t0 = time.perf_counter()
    gitlab_url = os.getenv("INTERNAL_GITLAB_URL", "").strip()
    ssl_verify = os.getenv("INTERNAL_GIT_SSL_VERIFY", "true").strip().lower() != "false"
    if gitlab_url:
        tls_status: CheckStatus = "healthy" if ssl_verify else "warning"
        tls_detail = f"사내 GitLab 연동 활성 ({gitlab_url}), TLS 검증={'ON' if ssl_verify else 'OFF'}"
        tls_rem = "" if ssl_verify else "운영 Pod에서는 INTERNAL_GIT_CA_BUNDLE을 마운트하고 SSL 검증을 켜세요."
    else:
        tls_status = "healthy"
        tls_detail = "로컬 개발 폴더 / 오프라인 안전 모드 준비 완료 (INTERNAL_GITLAB_URL 미지정)"
        tls_rem = "사내 GitLab을 연결하려면 `.env`에 INTERNAL_GITLAB_URL과 토큰을 설정하세요."
    checks.append(
        ServiceCheck(
            id="chk-gitlab",
            name="Internal GitLab & TLS Config",
            category="Security",
            status=tls_status,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail=tls_detail,
            remediation=tls_rem,
        )
    )

    return checks


def summarize_checks(checks: list[ServiceCheck]) -> dict[str, Any]:
    healthy = sum(1 for c in checks if c.status == "healthy")
    warning = sum(1 for c in checks if c.status == "warning")
    critical = sum(1 for c in checks if c.status == "critical")
    overall: CheckStatus = "critical" if critical > 0 else ("warning" if warning > 0 else "healthy")
    return {
        "total": len(checks),
        "healthy": healthy,
        "warning": warning,
        "critical": critical,
        "overall_status": overall,
    }


def render_dashboard_html(
    checks: list[ServiceCheck],
    *,
    repo_name: str = "Workspace",
    instruction: str = "",
) -> str:
    """Render a 100% standalone interactive HTML5 Operations Inspection Dashboard."""
    summary = summarize_checks(checks)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = {
        "repo_name": repo_name,
        "generated_at": generated_at,
        "snapshot_mode": "static_readonly",
        "summary": summary,
        "checks": [c.to_dict() for c in checks],
    }
    safe_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    safe_repo = html.escape(repo_name)
    safe_sub = html.escape(instruction.strip() or "실시간 서비스/저장소 상태 점검 대시보드")
    safe_generated_at = html.escape(generated_at)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_repo} — Service Inspection Dashboard</title>
  <style>
    :root {{
      --bg: #0c1222; --surface: #131c33; --border: #253457; --text: #edf2ff; --muted: #93a4cc;
      --healthy: #22c55e; --warning: #f59e0b; --critical: #ef4444; --accent: #38bdf8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", sans-serif;
      background: var(--bg); color: var(--text);
    }}
    .wrap {{ max-width: 1080px; margin: 0 auto; padding: 18px 20px 30px; }}
    header {{
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px;
      background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 16px 20px;
    }}
    h1 {{ margin: 0; font-size: 1.3rem; }}
    .sub {{ margin: 4px 0 0; color: var(--muted); font-size: 0.86rem; }}
    .btn {{
      background: var(--accent); color: #05131f; border: none; border-radius: 10px;
      padding: 8px 14px; font-weight: 700; cursor: pointer; font-size: 0.85rem;
    }}
    .kpis {{ display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px; margin: 14px 0; }}
    @media (max-width: 680px) {{ .kpis {{ grid-template-columns: repeat(2, 1fr); }} }}
    .kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; }}
    .kpi .v {{ font-size: 1.5rem; font-weight: 800; }}
    .kpi.healthy .v {{ color: var(--healthy); }}
    .kpi.warning .v {{ color: var(--warning); }}
    .kpi.critical .v {{ color: var(--critical); }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; align-items: center; }}
    .filter-btn {{
      background: var(--surface); color: var(--text); border: 1px solid var(--border);
      padding: 6px 12px; border-radius: 999px; cursor: pointer; font-size: 0.8rem;
    }}
    .filter-btn.active {{ background: var(--accent); color: #05131f; border-color: var(--accent); font-weight: 700; }}
    .row {{
      display: grid; grid-template-columns: 110px 1fr 90px; gap: 12px; align-items: center;
      background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
      padding: 12px 14px; margin-bottom: 8px; cursor: pointer;
    }}
    .row:hover {{ border-color: var(--accent); }}
    .pill {{
      display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.74rem; font-weight: 700; text-align: center;
    }}
    .pill.healthy {{ background: rgba(34,197,94,.16); color: var(--healthy); }}
    .pill.warning {{ background: rgba(245,158,11,.18); color: var(--warning); }}
    .pill.critical {{ background: rgba(239,68,68,.18); color: var(--critical); }}
    #detail-box {{
      margin-top: 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>🩺 {safe_repo} — Service Inspection Dashboard</h1>
        <p class="sub">{safe_sub} · <span id="last-run">스냅샷 측정 시각: {safe_generated_at}</span></p>
      </div>
      <button id="rerun-btn" class="btn" type="button">🔄 Re-run Health Check</button>
    </header>

    <section class="kpis">
      <div class="kpi"><div id="kpi-total" class="v">{summary["total"]}</div><div class="sub">전체 점검 항목</div></div>
      <div class="kpi healthy"><div id="kpi-healthy" class="v">{summary["healthy"]}</div><div class="sub">정상 (Healthy)</div></div>
      <div class="kpi warning"><div id="kpi-warning" class="v">{summary["warning"]}</div><div class="sub">주의 (Warning)</div></div>
      <div class="kpi critical"><div id="kpi-critical" class="v">{summary["critical"]}</div><div class="sub">심각 (Critical)</div></div>
    </section>

    <div class="toolbar">
      <button type="button" class="filter-btn active" data-filter="all">전체</button>
      <button type="button" class="filter-btn" data-filter="healthy">Healthy</button>
      <button type="button" class="filter-btn" data-filter="warning">Warning</button>
      <button type="button" class="filter-btn" data-filter="critical">Critical</button>
    </div>

    <div id="check-rows"></div>
    <div id="detail-box"><strong>점검 항목을 클릭하면 상세 진단과 조치 가이드를 보여줍니다.</strong></div>
  </div>

  <script id="inspection-data" type="application/json">{safe_json}</script>
  <script>
    (function () {{
      const payload = JSON.parse(document.getElementById("inspection-data").textContent || "{{}}");
      const snapshotChecks = Object.freeze(
        (payload.checks || []).map((c) => Object.freeze(Object.assign({{}}, c)))
      );
      const generatedAt = payload.generated_at || "unknown";
      let filter = "all";
      const rowsEl = document.getElementById("check-rows");
      const detailEl = document.getElementById("detail-box");
      const lastRunEl = document.getElementById("last-run");

      function esc(s) {{
        return String(s || "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#39;");
      }}

      function recomputeKpis() {{
        const total = snapshotChecks.length;
        const healthy = snapshotChecks.filter((c) => c.status === "healthy").length;
        const warning = snapshotChecks.filter((c) => c.status === "warning").length;
        const critical = snapshotChecks.filter((c) => c.status === "critical").length;
        document.getElementById("kpi-total").textContent = String(total);
        document.getElementById("kpi-healthy").textContent = String(healthy);
        document.getElementById("kpi-warning").textContent = String(warning);
        document.getElementById("kpi-critical").textContent = String(critical);
      }}

      function render() {{
        const list = snapshotChecks.filter((c) => filter === "all" || c.status === filter);
        rowsEl.innerHTML = list.length ? list.map((c) => (
          '<div class="row" data-id="' + esc(c.id) + '">' +
            '<span class="pill ' + esc(c.status) + '">' + esc(c.status.toUpperCase()) + '</span>' +
            '<div><strong>' + esc(c.name) + '</strong> <span class="sub">[' + esc(c.category) + ']</span><div class="sub">' + esc(c.detail) + '</div></div>' +
            '<div class="sub" style="text-align:right">' + Number(c.latency_ms).toFixed(1) + ' ms</div>' +
          '</div>'
        )).join("") : '<div class="row"><span></span><div>해당 상태의 항목이 없습니다.</div><span></span></div>';

        rowsEl.querySelectorAll(".row[data-id]").forEach((el) => {{
          el.addEventListener("click", () => {{
            const c = snapshotChecks.find((x) => x.id === el.getAttribute("data-id"));
            if (!c) return;
            detailEl.innerHTML =
              '<strong>' + esc(c.name) + ' (' + esc(c.status.toUpperCase()) + ')</strong>' +
              '<p class="sub">' + esc(c.detail) + '</p>' +
              '<p style="margin:6px 0 0;font-size:0.86rem;">조치 가이드: ' + esc(c.remediation || "추가 조치 불필요 — 정상 작동 중입니다.") + '</p>';
          }});
        }});
      }}

      document.querySelectorAll(".filter-btn").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          filter = btn.getAttribute("data-filter") || "all";
          document.querySelectorAll(".filter-btn").forEach((b) => b.classList.toggle("active", b === btn));
          render();
        }});
      }});

      document.getElementById("rerun-btn").addEventListener("click", () => {{
        filter = "all";
        document.querySelectorAll(".filter-btn").forEach((b) => {{
          b.classList.toggle("active", b.getAttribute("data-filter") === "all");
        }});
        recomputeKpis();
        render();
        lastRunEl.textContent = "스냅샷 기준 재검증 완료 · 원본 측정: " + generatedAt;
        detailEl.innerHTML =
          '<strong>정적 스냅샷 안내 (샌드박스 Live Preview)</strong>' +
          '<p class="sub">이 화면은 빌드 시점에 실제 실행한 점검 스냅샷(' + esc(generatedAt) + ')을 변조 없이 그대로 표시합니다. ' +
          '측정값을 새로 갱신하려면 Worktree에서 <code>python -m inspection_dashboard.checker --root . --output inspection_dashboard/index.html</code>를 다시 실행하세요.</p>';
      }});

      recomputeKpis();
      render();
    }})();
  </script>
</body>
</html>
"""


def generate_inspection_dashboard(
    root_path: str | Path,
    output_html: str | Path,
    instruction: str = "",
    *,
    repo_name: str | None = None,
) -> dict[str, Any]:
    root = Path(root_path).resolve()
    display_name = (repo_name or root.name).strip() or root.name
    checks = run_health_checks(root, repo_name=display_name)
    summary = summarize_checks(checks)
    out = Path(output_html)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_dashboard_html(checks, repo_name=display_name, instruction=instruction),
        encoding="utf-8",
    )
    return {"output_html": str(out), "repo_name": display_name, **summary}


def _decode_b64_arg(encoded: str) -> str:
    if not encoded:
        return ""
    return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate standalone service inspection dashboard HTML.")
    parser.add_argument("--root", default=".", help="Workspace root to inspect.")
    parser.add_argument("--output", default="inspection_dashboard/index.html", help="Output HTML file path.")
    parser.add_argument("--instruction", default="", help="Optional instruction subtitle.")
    parser.add_argument("--instruction-b64", default="", help="Base64-encoded instruction subtitle.")
    parser.add_argument("--repo-name", default="", help="Optional display repository name.")
    parser.add_argument("--repo-name-b64", default="", help="Base64-encoded display repository name.")
    args = parser.parse_args()
    instruction = _decode_b64_arg(args.instruction_b64) if args.instruction_b64 else args.instruction
    repo_name = (_decode_b64_arg(args.repo_name_b64) if args.repo_name_b64 else args.repo_name) or None
    print(
        json.dumps(
            generate_inspection_dashboard(
                args.root,
                args.output,
                instruction=instruction,
                repo_name=repo_name,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
'''

INSPECTION_TEST_CODE = '''"""Unit tests for the generated `inspection_dashboard` checker and standalone HTML app."""

from __future__ import annotations

from pathlib import Path

from inspection_dashboard.checker import (
    generate_inspection_dashboard,
    run_health_checks,
    summarize_checks,
)


def test_health_checks_and_summary_run_against_workspace() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    checks = run_health_checks(repo_root)
    summary = summarize_checks(checks)

    assert len(checks) >= 4
    assert summary["total"] == len(checks)
    assert summary["healthy"] >= 2


def test_generate_inspection_dashboard_writes_self_contained_html(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out = tmp_path / "dashboard.html"
    res = generate_inspection_dashboard(repo_root, out, instruction="서비스 상태 점검 대시보드")

    assert res["total"] >= 4
    html_text = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_text
    assert "Re-run Health Check" in html_text
    assert "inspection-data" in html_text
    assert "generated_at" in html_text
    assert "runCount % 3" not in html_text
    assert "fonts.googleapis.com" not in html_text
    assert "localStorage" not in html_text
'''


# =============================================================================
# Act 2 Preset #3: `custom` Interactive Feature App Builder
# =============================================================================
CUSTOM_FEATURE_BUILDER_CODE = r'''"""
custom_feature/builder.py
Generates a standalone interactive HTML5 feature showcase tailored to the user's instruction
and the real files discovered in the target repository.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
from pathlib import Path
from typing import Any

IGNORE_DIRS = frozenset({".git", ".abyss", ".agents", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"})


def build_custom_preview(
    root_path: str | Path,
    output_html: str | Path,
    instruction: str,
    *,
    repo_name: str | None = None,
) -> dict[str, Any]:
    root = Path(root_path).resolve()
    display_name = (repo_name or root.name).strip() or root.name
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for fn in sorted(filenames):
            if fn.endswith((".py", ".md", ".html")):
                files.append((Path(dirpath) / fn).relative_to(root).as_posix())
    files = files[:40]
    safe_inst = html.escape(instruction.strip() or "커스텀 인터랙티브 웹 기능")
    safe_json = json.dumps({"repo": display_name, "instruction": instruction, "files": files}, ensure_ascii=False).replace("</", "<\\/")

    doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <title>{html.escape(display_name)} — Custom Feature Preview</title>
  <style>
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#0f172a; color:#f1f5f9; padding:20px; }}
    .box {{ max-width:860px; margin:0 auto; background:#1e293b; border:1px solid #334155; border-radius:14px; padding:18px 20px; }}
    input {{ width:100%; padding:9px 12px; border-radius:8px; border:1px solid #475569; background:#0f172a; color:#f1f5f9; margin:10px 0; }}
    li {{ padding:5px 0; font-family:ui-monospace,Menlo,monospace; font-size:0.86rem; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>✨ {html.escape(display_name)} — Custom Build Preview</h1>
    <p>요청: <strong>{safe_inst}</strong> (발견된 파일 {len(files)}개)</p>
    <input id="q" type="search" placeholder="파일 필터링..." />
    <ul id="list"></ul>
  </div>
  <script id="custom-data" type="application/json">{safe_json}</script>
  <script>
    (function () {{
      const d = JSON.parse(document.getElementById("custom-data").textContent || "{{}}");
      const list = document.getElementById("list");
      const q = document.getElementById("q");
      function esc(s) {{
        return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
      }}
      function render() {{
        const term = (q.value || "").toLowerCase();
        list.innerHTML = (d.files || []).filter((f) => f.toLowerCase().includes(term)).map((f) => "<li>" + esc(f) + "</li>").join("");
      }}
      q.addEventListener("input", render);
      render();
    }})();
  </script>
</body>
</html>
"""
    out = Path(output_html)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return {"output_html": str(out), "files_indexed": len(files)}


def _decode_b64_arg(encoded: str) -> str:
    if not encoded:
        return ""
    return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="custom_feature/index.html")
    parser.add_argument("--instruction", default="")
    parser.add_argument("--instruction-b64", default="")
    parser.add_argument("--repo-name", default="")
    parser.add_argument("--repo-name-b64", default="")
    args = parser.parse_args()
    instruction = _decode_b64_arg(args.instruction_b64) if args.instruction_b64 else args.instruction
    repo_name = (_decode_b64_arg(args.repo_name_b64) if args.repo_name_b64 else args.repo_name) or None
    print(
        json.dumps(
            build_custom_preview(args.root, args.output, instruction, repo_name=repo_name),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
'''

CUSTOM_FEATURE_TEST_CODE = '''"""Unit tests for custom_feature builder."""

from pathlib import Path
from custom_feature.builder import build_custom_preview


def test_custom_preview_builds_valid_html(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out = tmp_path / "custom.html"
    res = build_custom_preview(repo_root, out, instruction="Custom test app")
    assert res["files_indexed"] >= 1
    assert "<!DOCTYPE html>" in out.read_text(encoding="utf-8")
'''


@dataclass
class BuildTaskRecord:
    """Allowlisted preview & branch tracking record persisted under `.abyss/build_tasks.json`."""

    task_id: str
    preset: str
    branch_name: str
    base_branch: str
    repo_name: str
    source_type: str
    worktree_path: str
    main_checkout_root: str
    preview_rel_path: str
    changed_files: list[str]
    commit_hash: str
    pushed_to_remote: bool
    remote_url: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CodingTaskService:
    """
    Executes Act 2 isolated coding builds with Live HTML Preview and Act 4 read-only
    independent diff/branch reviews.
    """

    def __init__(
        self,
        settings: AppSettings | None = None,
        rotator: LLMProviderRotator | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.rotator = rotator or LLMProviderRotator()
        self.event_callback = event_callback
        self.git_resolver = InternalGitSourceResolver(self.settings)
        self.rubric = EvaluatorRubric()
        self.registry_path = self.settings.demo_workspace / ".abyss" / "build_tasks.json"
        self._registry_lock = threading.RLock()
        self._tasks: dict[str, BuildTaskRecord] = {}
        self._load_registry()

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(event)

    def _load_registry(self) -> None:
        with self._registry_lock:
            if not self.registry_path.exists():
                return
            try:
                raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for item in raw:
                        rec = BuildTaskRecord(**item)
                        self._tasks[rec.task_id] = rec
            except Exception:  # noqa: BLE001
                self._tasks = {}

    def _save_registry(self) -> None:
        with self._registry_lock:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [rec.to_dict() for rec in self._tasks.values()]
            fd, tmp_name = tempfile.mkstemp(
                prefix=".build_tasks_",
                suffix=".tmp",
                dir=str(self.registry_path.parent),
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.registry_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

    def sanitize_error(self, exc: Exception) -> str:
        secrets = (self.settings.internal_gitlab_token,) if self.settings.internal_gitlab_token else ()
        return sanitize_git_text(str(exc), secrets)

    # =========================================================================
    # Canonical 4-Act Storyline Presets (`GET /api/workflow/tasks/presets`)
    # =========================================================================
    def get_presets(self) -> dict[str, Any]:
        return {
            "storyline_prompts": [
                {
                    "act": "Act 1",
                    "title": "낯선 프로젝트 구조 한눈에 파악하기",
                    "prompt": "이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘",
                    "endpoint": "POST /api/repo-onboard/analyze",
                },
                {
                    "act": "Act 2 (기본)",
                    "title": "인터랙티브 프로젝트 지도 웹 앱 만들기 + Live Preview",
                    "prompt": "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
                    "endpoint": "POST /api/workflow/tasks/build",
                    "preset": "project_map_app",
                },
                {
                    "act": "Act 2 (보조)",
                    "title": "서비스 상태 점검 대시보드 만들기 + Code Mode (REPL)",
                    "prompt": "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘",
                    "endpoint": "POST /api/workflow/tasks/build",
                    "preset": "inspection_dashboard",
                    "code_mode_endpoint": "POST /api/code-mode/run",
                    "scenario_id": "retry_policy",
                },
                {
                    "act": "Act 3",
                    "title": "위험 명령 & 비밀 파일 탈출 가드레일 차단",
                    "prompt": "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘",
                    "endpoint": "POST /api/code-mode/run",
                    "scenario_ids": ["compound_cmd_trap", "runtime_escape_and_hook"],
                },
                {
                    "act": "Act 4",
                    "title": "독립 코드 리뷰어가 보안·버그 지적 + 승인 검증",
                    "prompt": "방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘",
                    "endpoint": "POST /api/workflow/tasks/review",
                    "presets": ["defective_mr", "clean_branch"],
                },
            ],
            "build_presets": [
                {
                    "id": "project_map_app",
                    "label": "★ 프로젝트 구조 지도 웹 앱 만들기 (Act 2 추천)",
                    "instruction": "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
                    "preview_file": "project_map/index.html",
                    "badge": "Live Preview · AST Map",
                },
                {
                    "id": "inspection_dashboard",
                    "label": "서비스 상태 점검 대시보드 만들기 (보조 예시)",
                    "instruction": "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘",
                    "preview_file": "inspection_dashboard/index.html",
                    "badge": "Live Preview · Ops Health",
                },
                {
                    "id": "custom",
                    "label": "직접 지시로 웹 기능 만들기",
                    "instruction": "현재 저장소 파일 목록을 필터링해 보여주는 웹 페이지를 만들어줘",
                    "preview_file": "custom_feature/index.html",
                    "badge": "Custom Build",
                },
            ],
            "review_presets": [
                {
                    "id": "defective_mr",
                    "label": "Act 4-A · 결함 MR diff 리뷰 (비밀키·예외 삼킴·테스트 누락 탐지)",
                    "expected_decision": "REQUEST_CHANGES",
                },
                {
                    "id": "clean_branch",
                    "label": "Act 4-B · 방금 만든 Act 2 브랜치 자동 리뷰 (통과 검증)",
                    "expected_decision": "APPROVE",
                },
            ],
            "target_sources": [
                {"id": "demo", "label": "샘플 로컬 저장소 (demo)"},
                {"id": "self", "label": "현재 로컬 프로젝트 폴더 (self)"},
                {"id": "internal-origin", "label": "오프라인 테스트용 로컬 원격 (internal-origin)"},
            ],
        }

    # =========================================================================
    # Workspace Isolation Helper (Never Mutates User's Local Folder or `main`!)
    # =========================================================================
    @staticmethod
    def _copy_clean_snapshot(src_dir: Path, dst_dir: Path) -> None:
        dst_dir.mkdir(parents=True, exist_ok=True)
        copied_count = 0
        for dirpath, dirnames, filenames in os.walk(src_dir, followlinks=False):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _SNAPSHOT_IGNORE_DIRS and not (Path(dirpath) / d).is_symlink()
            ]
            rel_dir = Path(dirpath).relative_to(src_dir)
            target_dir = dst_dir / rel_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            for fn in filenames:
                if copied_count >= _SNAPSHOT_MAX_FILES:
                    return
                if fn.endswith((".pyc", ".pyo")):
                    continue
                src_file = Path(dirpath) / fn
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                try:
                    if src_file.stat().st_size > _SNAPSHOT_MAX_FILE_BYTES:
                        continue
                    shutil.copyfile(src_file, target_dir / fn)
                    copied_count += 1
                except OSError:
                    continue

    def _prepare_build_workspace(
        self, task_id: str, repo_url_or_path: str
    ) -> tuple[GitWorktreeManager, str, str, str]:
        """
        Returns `(worktree_manager, repo_name, source_type, sanitized_remote_url)`.

        Safety guarantees:
          - `demo` uses `demo_workspace` with an isolated worktree `.abyss/worktrees/task-<id>` (never touches `main`).
          - `self` or ANY plain non-git local folder is snapshotted into `task_runs/repo-<task_id>` with a fresh
            sandbox Git repo so the user's real directory and uncommitted working tree are NEVER touched.
          - External local Git repos are cloned into `task_runs/repo-<task_id>` (preserving their `origin` URL
            when present) so their working tree on `main` is NEVER touched.
          - Remote URLs (`internal-origin`, `file://`, internal GitLab HTTPS/SSH) are cloned via `InternalGitSourceResolver`.
        """
        cleaned = (repo_url_or_path or "demo").strip()
        if cleaned == "internal-origin":
            # Ensure local bare fixture exists before cloning
            demo_mgr = GitWorktreeManager(self.settings.demo_workspace)
            demo_mgr.ensure_internal_bare_remote(self.settings.internal_bare_remote)

        resolved_target, source_type, _display = self.git_resolver.resolve_target_spec(cleaned)
        self.settings.task_runs_dir.mkdir(parents=True, exist_ok=True)

        # Case 1: Built-in demo workspace
        if source_type == "local_path" and Path(resolved_target).resolve() == self.settings.demo_workspace.resolve():
            mgr = GitWorktreeManager(self.settings.demo_workspace)
            if not mgr.get_remote_info()["configured"] and (self.settings.internal_bare_remote / "HEAD").exists():
                mgr.ensure_internal_bare_remote(self.settings.internal_bare_remote)
            return mgr, self.settings.demo_workspace.name, "local_path", mgr.get_remote_info()["url"]

        # Case 2: Remote Git (`internal-origin`, `file://`, internal GitLab HTTPS/SSH)
        if source_type != "local_path":
            cloned = self.git_resolver.resolve(
                cleaned,
                shallow=False,
                fetch_all_branches=True,
                scratch_parent=self.settings.task_runs_dir,
            )
            mgr = GitWorktreeManager(cloned.local_path)
            return mgr, cloned.repo_name, cloned.source_type, cloned.remote_url or ""

        # Case 3: Local folder (`self`, external local git repo, OR plain non-git local folder!)
        local_src = Path(resolved_target).resolve()
        isolated_repo = (self.settings.task_runs_dir / f"repo-{task_id}").resolve()
        if isolated_repo.exists():
            shutil.rmtree(isolated_repo, ignore_errors=True)

        is_git = (local_src / ".git").exists()
        is_self_root = local_src == self.settings.repo_root.resolve()

        if is_git and not is_self_root:
            # Check if external local repo is clean and has commits; clone into isolated_repo and preserve its origin!
            origin_url = self.git_resolver._read_local_origin_url(local_src)
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(local_src),
                capture_output=True,
                text=True,
                check=False,
            )
            head_proc = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=str(local_src),
                capture_output=True,
                text=True,
                check=False,
            )
            if head_proc.returncode == 0 and not status_proc.stdout.strip():
                clone_proc = subprocess.run(
                    ["git", "clone", "--no-hardlinks", "--", str(local_src), str(isolated_repo)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if clone_proc.returncode == 0:
                    mgr = GitWorktreeManager(isolated_repo)
                    if origin_url:
                        mgr._run_git(["remote", "set-url", "origin", origin_url])
                    else:
                        mgr._run_git(["remote", "remove", "origin"])
                    return mgr, local_src.name, "local_path", origin_url or ""

        # Snapshot copy (handles `self` with live working tree AND plain non-git local folders!)
        self._copy_clean_snapshot(local_src, isolated_repo)
        mgr = GitWorktreeManager(isolated_repo)
        mgr._run_git(["add", "-A"])
        if mgr._run_git(["status", "--porcelain"]).stdout.strip():
            mgr._run_git(["commit", "-m", f"chore: snapshot '{local_src.name}' for isolated task build"])
        return mgr, local_src.name, "local_path", ""

    def _collect_runtime_secrets(self) -> tuple[str, ...]:
        secrets: list[str] = list(self.git_resolver._secrets())
        for key, val in os.environ.items():
            if _SENSITIVE_ENV_KEY_RE.search(key):
                cleaned = val.strip()
                if len(cleaned) >= 6 and cleaned not in secrets:
                    secrets.append(cleaned)
        return tuple(secrets)

    # =========================================================================
    # Act 2 Visual Build Execution (`POST /api/workflow/tasks/build`)
    # =========================================================================
    def _preset_blueprint(
        self,
        preset: Literal["project_map_app", "inspection_dashboard", "custom"],
        instruction: str,
        repo_name: str = "Workspace",
    ) -> dict[str, Any]:
        inst_b64 = _b64_arg(instruction)
        repo_b64 = _b64_arg(repo_name)
        safe_title = re.sub(r"[\r\n\t]+", " ", instruction).strip()[:48]
        if preset == "inspection_dashboard":
            return {
                "files": [
                    ("inspection_dashboard/__init__.py", INSPECTION_INIT_CODE),
                    ("inspection_dashboard/checker.py", INSPECTION_CHECKER_CODE),
                    ("tests/test_inspection_dashboard.py", INSPECTION_TEST_CODE),
                ],
                "build_cmd": (
                    f"python -m inspection_dashboard.checker --root . "
                    f"--output inspection_dashboard/index.html "
                    f"--instruction-b64={inst_b64} --repo-name-b64={repo_b64}"
                ),
                "test_file": "tests/test_inspection_dashboard.py",
                "preview_rel_path": "inspection_dashboard/index.html",
                "commit_msg": "feat(inspection-dashboard): add real workspace health checker & interactive HTML dashboard",
            }
        if preset == "custom":
            return {
                "files": [
                    ("custom_feature/__init__.py", '"""Custom feature preview package."""\n'),
                    ("custom_feature/builder.py", CUSTOM_FEATURE_BUILDER_CODE),
                    ("tests/test_custom_feature.py", CUSTOM_FEATURE_TEST_CODE),
                ],
                "build_cmd": (
                    f"python -m custom_feature.builder --root . "
                    f"--output custom_feature/index.html "
                    f"--instruction-b64={inst_b64} --repo-name-b64={repo_b64}"
                ),
                "test_file": "tests/test_custom_feature.py",
                "preview_rel_path": "custom_feature/index.html",
                "commit_msg": f"feat(custom): implement interactive preview for '{safe_title}'",
            }
        # Default: project_map_app
        return {
            "files": [
                ("project_map/__init__.py", PROJECT_MAP_INIT_CODE),
                ("project_map/builder.py", PROJECT_MAP_BUILDER_CODE),
                ("tests/test_project_map.py", PROJECT_MAP_TEST_CODE),
            ],
            "build_cmd": (
                f"python -m project_map.builder --root . "
                f"--output project_map/index.html "
                f"--instruction-b64={inst_b64} --repo-name-b64={repo_b64}"
            ),
            "test_file": "tests/test_project_map.py",
            "preview_rel_path": "project_map/index.html",
            "commit_msg": "feat(project-map): add AST repository scanner & interactive HTML project map",
        }

    def _run_build_sync(
        self,
        *,
        repo_url_or_path: str,
        instruction: str,
        preset: Literal["project_map_app", "inspection_dashboard", "custom"],
        base_branch: str | None,
        use_code_mode: bool,
        push_to_remote: bool,
    ) -> dict[str, Any]:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        branch_name = f"agent/{task_id}"

        validated_base: str | None = None
        if base_branch is not None and base_branch.strip():
            validated_base = _validate_git_ref(base_branch, label="base_branch")

        mgr, repo_name, source_type, remote_url = self._prepare_build_workspace(task_id, repo_url_or_path)
        blueprint = self._preset_blueprint(preset, instruction, repo_name=repo_name)

        effective_base = validated_base
        if effective_base is not None:
            verify_base = mgr._run_git(["rev-parse", "--verify", effective_base])
            if verify_base.returncode != 0:
                # Fall back to repo's actual default branch when caller kept default "main" on a "master"/custom repo
                if effective_base == "main":
                    effective_base = mgr.get_default_branch()
                else:
                    raise ValueError(f"저장소에서 기준 브랜치 '{effective_base}'를 찾을 수 없습니다.")

        wt_info = mgr.create_worktree(
            agent_id=task_id,
            base_branch=effective_base,
            branch_name=branch_name,
        )

        self._emit(
            {
                "agent_id": "builder",
                "role": "builder",
                "branch": branch_name,
                "event_type": "build_task_started",
                "task_id": task_id,
                "preset": preset,
                "repo_name": repo_name,
                "worktree_path": str(wt_info.worktree_path),
            }
        )

        registry, schemas = build_agent_tools(
            worktree_root=wt_info.worktree_path,
            agent_id=task_id,
            main_checkout_root=mgr.repo_root,
        )

        code_mode_executor: CodeModeExecutor | None = None
        if use_code_mode:
            code_mode_executor = CodeModeExecutor(
                worktree_root=wt_info.worktree_path,
                main_checkout_root=mgr.repo_root,
                permissions=PermissionRules.default(),
                event_callback=self.event_callback,
                agent_id=task_id,
            )

        runtime = AgentRuntime(
            agent_id=task_id,
            role="builder",
            worktree_root=wt_info.worktree_path,
            main_checkout_root=mgr.repo_root,
            worktree_branch=branch_name,
            tools_registry=registry,
            tools_schema=schemas,
            max_steps=8,
            event_callback=self.event_callback,
            code_mode=code_mode_executor,
            permissions=PermissionRules.default(),
        )

        files_to_write: list[tuple[str, str]] = blueprint["files"]
        build_cmd: str = blueprint["build_cmd"]
        test_file: str = blueprint["test_file"]
        preview_rel_path: str = blueprint["preview_rel_path"]

        turn = 0

        def scripted_build_responder(
            _messages: list[dict[str, Any]], _tools: list[dict[str, Any]]
        ) -> dict[str, Any]:
            nonlocal turn
            turn += 1
            if use_code_mode:
                if turn == 1:
                    lines: list[str] = []
                    for _idx, (fpath, fcontent) in enumerate(files_to_write):
                        lines.append(f"await write({fpath!r}, {fcontent!r})")
                    lines.append(f"build_out = await sh({build_cmd!r})")
                    lines.append(
                        f'test_out = await sh("pytest -p no:cacheprovider {test_file} -v", nothrow=True)'
                    )
                    lines.append('{"build_ok": build_out.ok, "test_ok": test_out.ok, "test_tail": test_out.tail(6)}')
                    return {
                        "content": f"<think>[Builder · Code Mode REPL] 1-Step REPL로 {preset} 소스 작성, 실제 AST/점검 HTML 생성 및 pytest 검증을 일괄 수행합니다.</think>",
                        "tool_calls": [
                            {
                                "id": f"call_{task_id}_repl",
                                "type": "function",
                                "function": {
                                    "name": "REPL",
                                    "arguments": json.dumps({"code": "\n".join(lines)}),
                                },
                            }
                        ],
                    }
                return {
                    "content": f"Built `{preset}` with live HTML preview `{preview_rel_path}` in 1 Code Mode REPL turn.",
                    "tool_calls": [],
                }

            if turn == 1:
                return {
                    "content": (
                        f"<think>[Builder] 격리된 Worktree(`{branch_name}`)에 `{preset}` 모듈과 단위 테스트를 작성합니다.</think>\n"
                        f"Writing `{preset}` modules and unit tests inside isolated worktree."
                    ),
                    "tool_calls": [
                        {
                            "id": f"call_{task_id}_write_{idx}",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": fpath, "content": fcontent}),
                            },
                        }
                        for idx, (fpath, fcontent) in enumerate(files_to_write)
                    ],
                }
            if turn == 2:
                return {
                    "content": (
                        f"<think>[Builder] 작성한 Python 빌더를 실제 실행해 `{preview_rel_path}`를 동적 생성하고 `pytest`를 실행합니다.</think>\n"
                        f"Executing generator to build `{preview_rel_path}` and verifying with pytest."
                    ),
                    "tool_calls": [
                        {
                            "id": f"call_{task_id}_gen",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps({"command": build_cmd}),
                            },
                        },
                        {
                            "id": f"call_{task_id}_pytest",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps(
                                    {"command": f"pytest -p no:cacheprovider {test_file} -v"}
                                ),
                            },
                        },
                    ],
                }
            return {
                "content": (
                    f"<think>[Builder] `{preview_rel_path}` 생성 및 단위 테스트 통과를 확인했습니다.</think>\n"
                    f"Completed `{preset}` in isolated branch `{branch_name}` with live preview `{preview_rel_path}`."
                ),
                "tool_calls": [],
            }

        def llm_bridge(
            messages: list[dict[str, Any]], tools_schema: list[dict[str, Any]]
        ) -> dict[str, Any]:
            step_reply = scripted_build_responder(messages, tools_schema)
            rot_res = self.rotator.invoke_chat(
                agent_id=task_id,
                role="builder",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a coding agent building a verified visual feature inside an isolated git worktree.",
                    },
                    {
                        "role": "user",
                        "content": f"In 1 concise sentence, summarize your build step for: {instruction}",
                    },
                ],
                tools_schema=None,
                mock_responder=lambda _m, _t: step_reply,
            )
            provider_tag = rot_res.get("provider", "mock:deterministic-simulator")
            live_note = (rot_res.get("content") or "").strip()
            merged_content = step_reply["content"]
            if live_note and live_note not in merged_content and not provider_tag.startswith("mock:"):
                merged_content = f"<think>[Live LLM ({provider_tag})]: {live_note}</think>\n{merged_content}"
            return {
                "content": merged_content,
                "tool_calls": step_reply["tool_calls"],
                "provider": provider_tag,
            }

        handoff = runtime.run(
            system_prompt=f"You are an isolated coding agent working in {wt_info.worktree_path} on branch {branch_name}.",
            task_prompt=instruction,
            llm_callable=llm_bridge,
        )

        # Authoritative pytest verification inside the isolated worktree (cache disabled, secrets scrubbed)
        pytest_env = _build_safe_subprocess_env(wt_info.worktree_path)
        pytest_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                test_file,
                "-v",
            ],
            cwd=str(wt_info.worktree_path),
            env=pytest_env,
            capture_output=True,
            text=True,
            check=False,
        )
        raw_pytest_output = ((pytest_proc.stdout or "") + (pytest_proc.stderr or "")).strip()
        pytest_output = sanitize_git_text(raw_pytest_output, secrets=self._collect_runtime_secrets())
        tests_passed = pytest_proc.returncode == 0

        # Commit changes cleanly inside the worktree branch (never mutates main!)
        commit_hash = mgr.commit_worktree_changes(task_id, blueprint["commit_msg"])
        git_diff = mgr.get_branch_diff(wt_info.base_branch, branch_name)
        changed_files = mgr.get_branch_changed_files(wt_info.base_branch, branch_name)

        pushed_to_remote = False
        final_remote_url = remote_url
        if push_to_remote and mgr.get_remote_info()["configured"]:
            push_res = mgr.push_branch(branch_name, settings=self.settings)
            pushed_to_remote = bool(push_res.get("pushed"))
            final_remote_url = str(push_res.get("remote_url") or final_remote_url)

        preview_disk = (wt_info.worktree_path / preview_rel_path).resolve()
        has_preview = preview_disk.is_file() and preview_disk.is_relative_to(wt_info.worktree_path.resolve())
        preview_url = f"/api/workflow/tasks/{task_id}/preview" if has_preview else None

        guardrail_checks: list[dict[str, Any]] = [
            {
                "step": rec.step,
                "tool_name": rec.tool_name,
                "check_name": rec.isolation_verdict.check_name,
                "allowed": rec.isolation_verdict.allowed,
                "reason": rec.isolation_verdict.reason,
            }
            for rec in handoff.tool_history
        ]
        if code_mode_executor and code_mode_executor.last_result:
            for inner in code_mode_executor.last_result.tool_calls:
                guardrail_checks.append(
                    {
                        "step": inner.seq,
                        "tool_name": f"REPL:{inner.tool}",
                        "check_name": inner.decided_by,
                        "allowed": inner.allowed,
                        "reason": inner.reason,
                    }
                )

        record = BuildTaskRecord(
            task_id=task_id,
            preset=preset,
            branch_name=branch_name,
            base_branch=wt_info.base_branch,
            repo_name=repo_name,
            source_type=source_type,
            worktree_path=str(wt_info.worktree_path),
            main_checkout_root=str(mgr.repo_root),
            preview_rel_path=preview_rel_path,
            changed_files=changed_files,
            commit_hash=commit_hash,
            pushed_to_remote=pushed_to_remote,
            remote_url=final_remote_url,
            created_at=time.time(),
        )
        with self._registry_lock:
            self._tasks[task_id] = record
            self._save_registry()

        status: Literal["completed", "failed"] = "completed" if (tests_passed and has_preview) else "failed"
        summary = (
            f"격리 브랜치 `{branch_name}`에서 `{preset}` 앱과 단위 테스트({len(changed_files)}개 파일)를 구현하고 "
            f"pytest 통과 후 라이브 프리뷰(`{preview_rel_path}`)를 생성했습니다."
            if status == "completed"
            else f"`{preset}` 빌드 중 검증이 실패했습니다 (pytest exit={pytest_proc.returncode})."
        )

        self._emit(
            {
                "agent_id": "builder",
                "role": "builder",
                "branch": branch_name,
                "event_type": "build_task_completed",
                "task_id": task_id,
                "status": status,
                "preview_url": preview_url,
                "pushed_to_remote": pushed_to_remote,
            }
        )

        return {
            "task_id": task_id,
            "preset": preset,
            "status": status,
            "repo_name": repo_name,
            "source_type": source_type,
            "branch_name": branch_name,
            "base_branch": wt_info.base_branch,
            "worktree_path": str(wt_info.worktree_path),
            "commit_hash": commit_hash,
            "changed_files": changed_files,
            "git_diff": git_diff[:32000],
            "test_result": {
                "exit_code": pytest_proc.returncode,
                "passed": tests_passed,
                "output": pytest_output[-2000:],
            },
            "guardrail_checks": guardrail_checks,
            "pushed_to_remote": pushed_to_remote,
            "remote_url": final_remote_url,
            "preview_url": preview_url,
            "preview_file": preview_rel_path if has_preview else None,
            "provider_used": handoff.provider_used,
            "summary": summary,
        }

    async def run_build_task(
        self,
        *,
        repo_url_or_path: str = "demo",
        instruction: str = "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
        preset: Literal["project_map_app", "inspection_dashboard", "custom"] = "project_map_app",
        base_branch: str | None = "main",
        use_code_mode: bool = False,
        push_to_remote: bool = True,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._run_build_sync,
            repo_url_or_path=repo_url_or_path,
            instruction=instruction,
            preset=preset,
            base_branch=base_branch,
            use_code_mode=use_code_mode,
            push_to_remote=push_to_remote,
        )

    # =========================================================================
    # Live Preview Resolver (`GET /api/workflow/tasks/{task_id}/preview`)
    # =========================================================================
    def get_preview_html(self, task_id: str) -> tuple[str, str]:
        """
        Strict allowlist + WorktreeIsolationPolicy containment check for Live Preview.
        Returns `(html_content, branch_name)`.
        Raises `KeyError` (→ 404) or `PermissionError` (→ 403).
        """
        if not task_id or not _TASK_ID_RE.fullmatch(task_id):
            raise KeyError(f"Unknown or invalid preview task_id: {task_id!r}")

        with self._registry_lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                self._load_registry()
                rec = self._tasks.get(task_id)
        if rec is None:
            raise KeyError(f"Preview task '{task_id}' not found.")

        wt_root = Path(rec.worktree_path).resolve()
        main_root = Path(rec.main_checkout_root).resolve()
        policy = WorktreeIsolationPolicy(worktree_root=wt_root, main_checkout_root=main_root)
        verdict = policy.validate_file_edit(rec.preview_rel_path)
        if not verdict.allowed:
            raise PermissionError(verdict.reason)

        target_file = (wt_root / rec.preview_rel_path).resolve()
        if not target_file.is_relative_to(wt_root) or target_file.suffix.lower() != ".html":
            raise PermissionError("Preview artifact must be an .html file inside the task worktree.")
        if not target_file.is_file():
            raise KeyError(f"Preview file '{rec.preview_rel_path}' does not exist on disk.")

        return target_file.read_text(encoding="utf-8", errors="replace"), rec.branch_name

    # =========================================================================
    # Act 4 Independent Read-Only Review (`POST /api/workflow/tasks/review`)
    # =========================================================================
    def _find_record_by_branch(self, branch_name: str | None) -> BuildTaskRecord | None:
        with self._registry_lock:
            if not self._tasks:
                self._load_registry()
            if not self._tasks:
                return None
            if branch_name:
                for rec in reversed(list(self._tasks.values())):
                    if rec.branch_name == branch_name:
                        return rec
                return None
            return max(self._tasks.values(), key=lambda r: r.created_at)

    def _run_readonly_pytest_on_branch(
        self, repo_root: Path, branch_name: str
    ) -> dict[str, Any]:
        """Run pytest strictly read-only in a detached ephemeral worktree (never mutates target branch or worktree)."""
        ephemeral_wt: Path | None = None
        try:
            ephemeral_wt = Path(
                tempfile.mkdtemp(prefix="rev_wt_", dir=str(self.settings.task_runs_dir))
            )
            shutil.rmtree(ephemeral_wt, ignore_errors=True)
            add_res = subprocess.run(
                ["git", "worktree", "add", "--detach", str(ephemeral_wt), branch_name],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if add_res.returncode != 0:
                safe_err = sanitize_git_text(
                    add_res.stderr.strip(), secrets=self._collect_runtime_secrets()
                )
                return {
                    "exit_code": add_res.returncode,
                    "passed": False,
                    "output": f"Could not checkout detached review worktree: {safe_err}",
                }
            run_dir = ephemeral_wt

            tests_dir = run_dir / "tests"
            if not tests_dir.exists() or not list(tests_dir.rglob("test_*.py")):
                return {
                    "exit_code": 0,
                    "passed": True,
                    "output": "No pytest test files discovered in branch.",
                }

            env = _build_safe_subprocess_env(run_dir)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "tests",
                    "-q",
                ],
                cwd=str(run_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            raw_out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            safe_out = sanitize_git_text(raw_out, secrets=self._collect_runtime_secrets())
            return {
                "exit_code": proc.returncode,
                "passed": proc.returncode == 0,
                "output": safe_out[-2000:],
            }
        finally:
            if ephemeral_wt is not None:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(ephemeral_wt)],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if ephemeral_wt.exists():
                    shutil.rmtree(ephemeral_wt, ignore_errors=True)

    def _run_review_sync(
        self,
        *,
        repo_url_or_path: str,
        branch_name: str | None,
        base_branch: str,
        commit_range: str | None,
        raw_diff: str | None,
        preset: str | None,
        run_tests: bool,
    ) -> dict[str, Any]:
        review_id = f"rev-{uuid.uuid4().hex[:8]}"
        self.settings.task_runs_dir.mkdir(parents=True, exist_ok=True)
        effective_preset = preset
        source_type = "raw_diff"
        target_label = "raw_diff"
        diff_text = ""
        test_result: dict[str, Any] | None = None

        # Validate any caller-supplied Git refs/ranges upfront (blocks Git flag injection)
        validated_branch: str | None = (
            _validate_git_ref(branch_name, label="branch_name")
            if branch_name is not None and branch_name.strip()
            else None
        )
        validated_base = _validate_git_ref(base_branch or "main", label="base_branch")
        validated_range: str | None = (
            _validate_commit_range(commit_range)
            if commit_range is not None and commit_range.strip()
            else None
        )

        # Mode A: Direct raw_diff OR Defective MR preset (Act 4-A)
        if raw_diff is not None and raw_diff.strip():
            diff_text = raw_diff
            source_type = "raw_diff"
            target_label = "raw_diff"
        elif effective_preset in ("defective_mr", "defective_dashboard_mr"):
            diff_text = DEFECTIVE_MR_SAMPLE_DIFF
            source_type = "preset_mr_diff"
            target_label = "preset:defective_mr (inspection_dashboard/api_handler.py)"
        else:
            # Mode B: Branch / Commit-range Read-Only Review (Act 4-B or custom branch)
            matched_rec = self._find_record_by_branch(validated_branch)
            if validated_branch is None and effective_preset in ("clean_branch", "latest_build_branch"):
                if matched_rec is None:
                    raise ValueError(
                        "리뷰할 빌드 브랜치가 아직 없습니다. 먼저 Act 2에서 웹 앱을 생성하거나 "
                        "리뷰할 `branch_name` 또는 `raw_diff`를 지정하세요."
                    )
                validated_branch = matched_rec.branch_name
                validated_base = matched_rec.base_branch

            if matched_rec is not None and validated_branch == matched_rec.branch_name:
                repo_root = Path(matched_rec.main_checkout_root).resolve()
                source_type = matched_rec.source_type
                diff_spec = validated_range or f"{matched_rec.base_branch}...{matched_rec.branch_name}"
                target_label = f"{matched_rec.repo_name}:{diff_spec}"
                diff_proc = subprocess.run(
                    ["git", "diff", "--end-of-options", diff_spec, "--"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if diff_proc.returncode != 0:
                    raise ValueError(
                        f"git diff 실패 ({diff_spec}): {diff_proc.stderr.strip() or 'invalid ref'}"
                    )
                diff_text = diff_proc.stdout
                if run_tests:
                    test_result = self._run_readonly_pytest_on_branch(
                        repo_root, matched_rec.branch_name
                    )
            else:
                with self.git_resolver.checkout(
                    repo_url_or_path or "demo",
                    shallow=False,
                    fetch_all_branches=True,
                    scratch_parent=self.settings.task_runs_dir,
                ) as resolved:
                    if not resolved.is_git_repo:
                        raise ValueError(
                            f"'{resolved.display_source}' 폴더는 Git 저장소가 아니므로 브랜치 diff를 계산할 수 없습니다. "
                            "Git 저장소 경로나 `raw_diff`를 입력하세요."
                        )
                    source_type = resolved.source_type
                    target_branch = validated_branch or "HEAD"
                    # Support remote-tracking branch fallback if cloned from bare/GitLab
                    branch_ref = target_branch
                    verify = subprocess.run(
                        ["git", "rev-parse", "--verify", "--end-of-options", target_branch],
                        cwd=str(resolved.local_path),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if verify.returncode != 0:
                        remote_ref = f"origin/{target_branch}"
                        verify_r = subprocess.run(
                            ["git", "rev-parse", "--verify", "--end-of-options", remote_ref],
                            cwd=str(resolved.local_path),
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if verify_r.returncode == 0:
                            branch_ref = remote_ref
                        else:
                            raise ValueError(f"저장소에서 브랜치 '{target_branch}'를 찾을 수 없습니다.")

                    base_ref = validated_base
                    verify_b = subprocess.run(
                        ["git", "rev-parse", "--verify", "--end-of-options", base_ref],
                        cwd=str(resolved.local_path),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if verify_b.returncode != 0:
                        if subprocess.run(
                            ["git", "rev-parse", "--verify", "--end-of-options", f"origin/{validated_base}"],
                            cwd=str(resolved.local_path),
                            capture_output=True,
                            text=True,
                            check=False,
                        ).returncode == 0:
                            base_ref = f"origin/{validated_base}"

                    diff_spec = validated_range or f"{base_ref}...{branch_ref}"
                    target_label = f"{resolved.repo_name}:{diff_spec}"
                    diff_proc = subprocess.run(
                        ["git", "diff", "--end-of-options", diff_spec, "--"],
                        cwd=str(resolved.local_path),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if diff_proc.returncode != 0:
                        raise ValueError(
                            f"git diff 실패 ({diff_spec}): {diff_proc.stderr.strip() or 'invalid ref'}"
                        )
                    diff_text = diff_proc.stdout
                    if run_tests and not validated_range:
                        test_result = self._run_readonly_pytest_on_branch(
                            resolved.local_path, branch_ref
                        )

        pytest_exit = test_result["exit_code"] if test_result is not None else None
        pytest_out = test_result["output"] if test_result is not None else ""
        report = self.rubric.analyze_diff_for_review(
            diff_text,
            pytest_exit_code=pytest_exit,
            pytest_output=pytest_out,
            require_tests=True,
        )

        # Invoke Reviewer LLM role via rotator (deterministic fallback when offline/mock)
        rot_res = self.rotator.invoke_chat(
            agent_id=review_id,
            role="reviewer",
            messages=[
                {
                    "role": "system",
                    "content": "You are an independent read-only code reviewer explaining findings clearly to beginners.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Review verdict: {report.decision} ({report.score}/100) for {target_label}. "
                        f"Findings count: {len(report.findings)}. Summarize in 1 short sentence."
                    ),
                },
            ],
            tools_schema=None,
            mock_responder=lambda _m, _t: {"content": report.summary, "tool_calls": []},
        )
        provider_used = str(rot_res.get("provider", "mock:deterministic-simulator"))
        live_summary = str(rot_res.get("content") or "").strip()
        final_summary = report.summary
        if live_summary and not provider_used.startswith("mock:") and live_summary != report.summary:
            final_summary = f"{report.summary} [LLM Reviewer ({provider_used}): {live_summary}]"

        self._emit(
            {
                "agent_id": "reviewer",
                "role": "reviewer",
                "event_type": "mr_review_completed",
                "review_id": review_id,
                "target": target_label,
                "decision": report.decision,
                "score": report.score,
                "findings_count": len(report.findings),
            }
        )

        return {
            "review_id": review_id,
            "source_type": source_type,
            "target": target_label,
            "decision": report.decision,
            "score": report.score,
            "summary": final_summary,
            "findings": [f.to_dict() for f in report.findings],
            "diff_stats": report.diff_stats.to_dict(),
            "test_result": test_result,
            "rubric_breakdown": [c.to_dict() for c in report.rubric_breakdown],
            "provider_used": provider_used,
            "read_only_verified": True,
        }

    async def run_review_task(
        self,
        *,
        repo_url_or_path: str = "demo",
        branch_name: str | None = None,
        base_branch: str = "main",
        commit_range: str | None = None,
        raw_diff: str | None = None,
        preset: str | None = None,
        run_tests: bool = True,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._run_review_sync,
            repo_url_or_path=repo_url_or_path,
            branch_name=branch_name,
            base_branch=base_branch,
            commit_range=commit_range,
            raw_diff=raw_diff,
            preset=preset,
            run_tests=run_tests,
        )
