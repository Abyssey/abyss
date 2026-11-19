"""
tests/test_coding_agent_tasks.py
End-to-End Verification Suite for Act 2 Visual Build (`project_map_app` default & `inspection_dashboard`),
Safe Live HTML Preview (`GET /api/workflow/tasks/{task_id}/preview`),
and Act 4 Independent Read-Only Code/MR Review (`POST /api/workflow/tasks/review`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import AppSettings
from app.main import create_app


def _make_isolated_test_app(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    demo_ws = tmp_path / "demo_workspace" / "repo_onboard"
    bare_remote = tmp_path / "demo_workspace" / "internal_origin.git"
    settings = AppSettings(
        repo_root=repo_root,
        static_dir=repo_root / "app" / "static",
        demo_workspace=demo_ws,
        code_mode_sandbox=demo_ws / ".abyss" / "code_mode_sandbox",
        internal_bare_remote=bare_remote,
        task_runs_dir=tmp_path / "demo_workspace" / "task_runs",
    )
    return create_app(settings=settings), settings


def test_presets_endpoint_exposes_5_canonical_korean_prompts(tmp_path: Path) -> None:
    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        res = client.get("/api/workflow/tasks/presets")
        assert res.status_code == 200
        data = res.json()
        prompts = data["storyline_prompts"]
        assert len(prompts) == 5
        assert prompts[0]["prompt"] == "이 프로젝트 구조랑 핵심 파일들 쉽게 설명해줘"
        assert prompts[1]["prompt"] == "이 프로젝트 구조 한눈에 보는 인터랙티브 웹 페이지 만들어줘"
        assert data["build_presets"][0]["id"] == "project_map_app"


def test_act2_project_map_build_live_preview_and_act4_clean_review(tmp_path: Path) -> None:
    test_app, settings = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        # 1. Act 2 Build (Default: `project_map_app`)
        build_res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": "demo",
                "instruction": "이 프로젝트 구조 한눈에 보는 인터랙티브 웹 페이지 만들어줘",
                "preset": "project_map_app",
                "use_code_mode": False,
                "push_to_remote": True,
            },
        )
        assert build_res.status_code == 200, build_res.text
        b = build_res.json()

        assert b["status"] == "completed"
        assert b["preset"] == "project_map_app"
        assert b["branch_name"].startswith("agent/task-")
        assert b["test_result"]["passed"] is True
        assert b["test_result"]["exit_code"] == 0
        assert isinstance(b["guardrail_checks"], list) and len(b["guardrail_checks"]) >= 3
        assert "project_map/builder.py" in b["changed_files"]
        assert "project_map/index.html" in b["changed_files"]
        assert "tests/test_project_map.py" in b["changed_files"]
        assert b["pushed_to_remote"] is True
        assert b["preview_url"] == f"/api/workflow/tasks/{b['task_id']}/preview"

        # Verify bare remote received the pushed feature branch
        ls_remote = subprocess.run(
            ["git", "ls-remote", "--heads", str(settings.internal_bare_remote)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert f"refs/heads/{b['branch_name']}" in ls_remote

        # Verify `main` checkout working tree was NOT polluted by build task
        main_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(settings.demo_workspace),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert main_status == ""
        assert not (settings.demo_workspace / "project_map").exists()

        # 2. Live Preview returns HTTP 200 self-contained HTML containing real scanned files
        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        assert "text/html" in prev.headers["content-type"]
        assert "<!DOCTYPE html>" in prev.text
        assert "repo_onboard/analyzer.py" in prev.text
        assert "detail-drawer" in prev.text
        assert "onboarding-checklist" in prev.text
        assert "fonts.googleapis.com" not in prev.text
        assert "localStorage" not in prev.text

        # Unknown & path traversal task_ids are refused with 404
        assert client.get("/api/workflow/tasks/nonexistent-task/preview").status_code == 404
        assert client.get("/api/workflow/tasks/..%2F..%2Fetc/preview").status_code == 404

        # 3. Act 4-B Independent Read-Only Review APPROVES the clean Act 2 branch
        head_before = subprocess.run(
            ["git", "rev-parse", b["branch_name"]],
            cwd=str(settings.demo_workspace),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        rev_res = client.post(
            "/api/workflow/tasks/review",
            json={
                "repo_url_or_path": "demo",
                "branch_name": b["branch_name"],
                "base_branch": "main",
                "run_tests": True,
            },
        )
        assert rev_res.status_code == 200, rev_res.text
        rev = rev_res.json()
        assert rev["decision"] == "APPROVE"
        assert rev["score"] >= 80
        assert rev["read_only_verified"] is True
        assert rev["test_result"]["passed"] is True
        assert rev["diff_stats"]["files_changed"] >= 3

        # Confirm strictly read-only: branch HEAD commit SHA is completely unchanged!
        head_after = subprocess.run(
            ["git", "rev-parse", b["branch_name"]],
            cwd=str(settings.demo_workspace),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_before == head_after


def test_act2_inspection_dashboard_with_code_mode_and_live_preview(tmp_path: Path) -> None:
    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": "demo",
                "instruction": "우리 서비스 상태 점검 대시보드 화면이랑 체크 코드 만들어줘",
                "preset": "inspection_dashboard",
                "use_code_mode": True,
                "push_to_remote": True,
            },
        )
        assert res.status_code == 200, res.text
        b = res.json()
        assert b["status"] == "completed"
        assert b["test_result"]["passed"] is True
        assert "inspection_dashboard/checker.py" in b["changed_files"]
        assert "inspection_dashboard/index.html" in b["changed_files"]
        assert any(c["tool_name"].startswith("REPL:") for c in b["guardrail_checks"])

        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        assert "Re-run Health Check" in prev.text
        assert "Service Inspection Dashboard" in prev.text


def test_act2_build_against_self_scans_real_modern_ddd_without_mutating_repo(tmp_path: Path) -> None:
    test_app, settings = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": "self",
                "instruction": "현재 코드베이스 구조 지도 만들어줘",
                "preset": "project_map_app",
                "push_to_remote": False,
            },
        )
        assert res.status_code == 200, res.text
        b = res.json()
        assert b["status"] == "completed"
        assert b["test_result"]["passed"] is True

        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        assert "app/domains/guardrails/isolation_policy.py" in prev.text
        assert "app/domains/execution/code_mode.py" in prev.text
        # Root repository must NOT be polluted by project_map/
        assert not (settings.repo_root / "project_map").exists()


def test_act4_defective_mr_review_requests_changes_with_file_line_findings(tmp_path: Path) -> None:
    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        # 1. Built-in Act 4-A defective MR preset
        res_preset = client.post(
            "/api/workflow/tasks/review",
            json={"preset": "defective_mr"},
        )
        assert res_preset.status_code == 200, res_preset.text
        rev = res_preset.json()
        assert rev["decision"] == "REQUEST_CHANGES"
        assert rev["score"] < 80
        assert rev["read_only_verified"] is True
        assert len(rev["findings"]) >= 3
        high_findings = [f for f in rev["findings"] if f["severity"] == "high"]
        assert len(high_findings) >= 2
        assert all(f["file"] == "inspection_dashboard/api_handler.py" and f["line"] > 0 for f in high_findings)
        assert any("비밀키" in f["comment"] or "하드코딩" in f["comment"] for f in high_findings)
        assert any("except" in f["comment"] for f in high_findings)

        # 2. Custom raw_diff input
        custom_diff = """diff --git a/billing/charge.py b/billing/charge.py
new file mode 100644
--- /dev/null
+++ b/billing/charge.py
@@ -0,0 +1,8 @@
+API_KEY = "sk-live-secret-999999"
+
+def charge(expr: str) -> int:
+    try:
+        return int(eval(expr))
+    except:
+        pass
+    return 0
"""
        res_raw = client.post(
            "/api/workflow/tasks/review",
            json={"raw_diff": custom_diff},
        )
        assert res_raw.status_code == 200
        raw_rev = res_raw.json()
        assert raw_rev["decision"] == "REQUEST_CHANGES"
        assert raw_rev["score"] < 60
        assert any(f["file"] == "billing/charge.py" and f["line"] == 1 for f in raw_rev["findings"])
        assert any("eval" in f["comment"] for f in raw_rev["findings"])
