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
    from app.domains.workflow.schemas import BuildTaskRequest

    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        res = client.get("/api/workflow/tasks/presets")
        assert res.status_code == 200
        data = res.json()
        prompts = data["storyline_prompts"]
        assert len(prompts) == 5
        assert [p["prompt"] for p in prompts] == [
            "이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘",
            "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
            "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘",
            "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘",
            "방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘",
        ]
        assert data["build_presets"][0]["id"] == "project_map_app"
        assert (
            data["build_presets"][0]["instruction"]
            == "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"
        )
        assert (
            data["build_presets"][1]["instruction"]
            == "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘"
        )
        assert (
            BuildTaskRequest().instruction
            == "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"
        )


def test_act2_project_map_build_live_preview_and_act4_clean_review(tmp_path: Path) -> None:
    test_app, settings = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        # 1. Act 2 Build (Default: `project_map_app`)
        build_res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": "demo",
                "instruction": "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
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
                "instruction": "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘",
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
        assert "generated_at" in prev.text
        assert "스냅샷 기준 재검증 완료" in prev.text
        assert "python -m inspection_dashboard.checker" in prev.text
        assert "runCount % 3" not in prev.text
        assert "latency_ms = Math.max" not in prev.text


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


def test_act2_build_instruction_prevents_shell_injection_env_leak_and_backtick_crash(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PROBE_SECRET_SENTINEL", "SECRET_POD_999888")
    marker = tmp_path / "rce_marker_should_not_exist"
    malicious_instruction = (
        f'Map $(touch {marker} && echo $PROBE_SECRET_SENTINEL) '
        f'$PROBE_SECRET_SENTINEL $100 `README.md`'
    )

    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        for preset, use_cm in (("project_map_app", False), ("inspection_dashboard", True)):
            res = client.post(
                "/api/workflow/tasks/build",
                json={
                    "repo_url_or_path": "demo",
                    "instruction": malicious_instruction,
                    "preset": preset,
                    "use_code_mode": use_cm,
                    "push_to_remote": False,
                },
            )
            assert res.status_code == 200, res.text
            b = res.json()
            assert b["status"] == "completed"
            assert not marker.exists(), "Shell injection executed via build instruction!"
            assert "SECRET_POD_999888" not in b["git_diff"]
            prev = client.get(b["preview_url"])
            assert prev.status_code == 200
            assert "SECRET_POD_999888" not in prev.text


def test_act4_review_rejects_git_flag_injection_in_commit_range_and_branches(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "git_output_pwned.txt"
    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        bad_payloads = [
            {"repo_url_or_path": "demo", "commit_range": f"--output={marker}", "run_tests": False},
            {"repo_url_or_path": "demo", "commit_range": "--stat", "run_tests": False},
            {"repo_url_or_path": "demo", "branch_name": "--upload-pack=id", "run_tests": False},
            {"repo_url_or_path": "demo", "base_branch": "-c foo=bar", "run_tests": False},
        ]
        for payload in bad_payloads:
            res = client.post("/api/workflow/tasks/review", json=payload)
            assert res.status_code == 400, f"Expected 400 for {payload}, got {res.status_code}"
        assert not marker.exists()


def test_act4_review_catches_prefixed_secret_vars_and_bare_except_variants(tmp_path: Path) -> None:
    test_app, _ = _make_isolated_test_app(tmp_path)
    tricky_diff = """diff --git a/service/auth.py b/service/auth.py
new file mode 100644
--- /dev/null
+++ b/service/auth.py
@@ -0,0 +1,19 @@
+GITHUB_TOKEN = "corp_internal_secret_987654321"
+SERVICE_API_KEY = "prod-key-abcdef9999"
+MYSQL_DB_PASSWORD = "super_secret_pw_123"
+
+def parse_one(x: str) -> str:
+    try:
+        return x.strip()
+    except:
+        # ignore
+        pass
+    return ""
+
+def parse_two(x: str):
+    try:
+        return int(x)
+    except:
+        return None
diff --git a/tests/test_auth.py b/tests/test_auth.py
new file mode 100644
--- /dev/null
+++ b/tests/test_auth.py
@@ -0,0 +1,2 @@
+def test_dummy():
+    assert True
"""
    with TestClient(test_app) as client:
        res = client.post("/api/workflow/tasks/review", json={"raw_diff": tricky_diff})
        assert res.status_code == 200
        rev = res.json()
        assert rev["decision"] == "REQUEST_CHANGES"
        assert rev["score"] < 80
        secret_lines = {f["line"] for f in rev["findings"] if "비밀키" in f["comment"] or "하드코딩" in f["comment"]}
        assert {1, 2, 3}.issubset(secret_lines)
        except_lines = {f["line"] for f in rev["findings"] if "except" in f["comment"]}
        assert len(except_lines) >= 2

        # Clean safe diff (TOKEN_TYPE = "Bearer" + logged exception) must APPROVE with 0 findings
        clean_diff = """diff --git a/service/safe.py b/service/safe.py
new file mode 100644
--- /dev/null
+++ b/service/safe.py
@@ -0,0 +1,9 @@
+TOKEN_TYPE = "Bearer"
+
+def safe_parse(x: str) -> int:
+    try:
+        return int(x)
+    except ValueError:
+        return 0
diff --git a/tests/test_safe.py b/tests/test_safe.py
new file mode 100644
--- /dev/null
+++ b/tests/test_safe.py
@@ -0,0 +1,2 @@
+def test_safe():
+    assert True
"""
        res_clean = client.post("/api/workflow/tasks/review", json={"raw_diff": clean_diff})
        assert res_clean.status_code == 200
        clean_rev = res_clean.json()
        assert clean_rev["decision"] == "APPROVE"
        assert clean_rev["findings"] == []


def test_preview_sets_csp_sandbox_nosniff_and_escapes_custom_preset_xss_filenames(
    tmp_path: Path,
) -> None:
    xss_folder = tmp_path / "xss_project"
    xss_folder.mkdir()
    (xss_folder / "<img src=x onerror=alert(1)>.py").write_text("X = 1\n", encoding="utf-8")

    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": str(xss_folder),
                "instruction": "XSS escape check",
                "preset": "custom",
                "push_to_remote": False,
            },
        )
        assert res.status_code == 200, res.text
        b = res.json()
        assert b["status"] == "completed"
        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        csp = prev.headers.get("content-security-policy", "")
        assert "sandbox" in csp and "allow-scripts" in csp
        assert prev.headers.get("x-content-type-options") == "nosniff"
        assert '"<li>" + f + "</li>"' not in prev.text
        assert "esc(f)" in prev.text


def test_build_commits_no_pyc_preserves_repo_name_and_survives_copystat_eperm(
    tmp_path: Path, monkeypatch
) -> None:
    import shutil
    from app.domains.workflow.schemas import BuildTaskRequest

    named_folder = tmp_path / "billing_portal_demo"
    named_folder.mkdir()
    (named_folder / "ledger.py").write_text(
        '"""Ledger engine."""\ndef balance() -> int:\n    return 100\n', encoding="utf-8"
    )

    def _forbid_copy2(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(shutil, "copy2", _forbid_copy2)

    req_model = BuildTaskRequest(base_branch="main")
    assert req_model.base_branch == "main"

    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": str(named_folder),
                "instruction": "빌링 포털 지도 만들어줘",
                "preset": "project_map_app",
                "base_branch": "main",
                "push_to_remote": False,
            },
        )
        assert res.status_code == 200, res.text
        b = res.json()
        assert b["status"] == "completed"
        assert all(
            not f.endswith(".pyc") and "__pycache__" not in f and ".pytest_cache" not in f
            for f in b["changed_files"]
        )
        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        assert "<title>billing_portal_demo — Interactive Project Map</title>" in prev.text


def test_r3_empty_instruction_inspection_nonzero_count_and_pytest_secret_scrub(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("INTERNAL_GITLAB_TOKEN", "CANARY_POD_GITLAB_77777")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "CANARY_POD_AWS_88888")

    sample_dir = tmp_path / "orders_gateway"
    sample_dir.mkdir()
    (sample_dir / "service.py").write_text(
        '"""Order gateway service."""\ndef ping() -> str:\n    return "ok"\n', encoding="utf-8"
    )
    (sample_dir / "models.py").write_text(
        '"""Order models."""\nclass Order:\n    id: int = 1\n', encoding="utf-8"
    )

    test_app, _ = _make_isolated_test_app(tmp_path)
    with TestClient(test_app) as client:
        # Empty instruction must succeed cleanly on inspection_dashboard and count real files (> 0)
        res = client.post(
            "/api/workflow/tasks/build",
            json={
                "repo_url_or_path": str(sample_dir),
                "instruction": "",
                "preset": "inspection_dashboard",
                "push_to_remote": False,
            },
        )
        assert res.status_code == 200, res.text
        b = res.json()
        assert b["status"] == "completed"
        assert b["test_result"]["passed"] is True
        assert "CANARY_POD_GITLAB_77777" not in b["test_result"]["output"]
        assert "CANARY_POD_AWS_88888" not in b["test_result"]["output"]

        prev = client.get(b["preview_url"])
        assert prev.status_code == 200
        assert "orders_gateway" in prev.text
        assert "Python 파일 0개" not in prev.text

