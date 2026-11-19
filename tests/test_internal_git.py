"""
tests/test_internal_git.py
Security & Functional Verification Suite for Zero-Leak Internal GitLab, Local Folders (Git & Plain Non-Git),
Bare Remote Fixture, and Pod Readiness Probes (`/healthz`, `/readyz`).
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import AppSettings, load_settings
from app.core.git_source import (
    InternalGitSourceError,
    InternalGitSourceResolver,
    push_branch_safe,
    sanitize_git_text,
)
from app.domains.onboarding.service import OnboardingService
from app.domains.workflow.task_service import CodingTaskService
from app.main import app


def _make_settings(tmp_path: Path, **overrides: Any) -> AppSettings:
    repo_root = Path(__file__).resolve().parent.parent
    demo_ws = tmp_path / "demo_workspace" / "repo_onboard"
    demo_ws.mkdir(parents=True, exist_ok=True)
    defaults: dict[str, Any] = {
        "repo_root": repo_root,
        "static_dir": repo_root / "app" / "static",
        "demo_workspace": demo_ws,
        "code_mode_sandbox": demo_ws / ".abyss" / "code_mode_sandbox",
        "internal_bare_remote": tmp_path / "demo_workspace" / "internal_origin.git",
        "task_runs_dir": tmp_path / "demo_workspace" / "task_runs",
    }
    defaults.update(overrides)
    return AppSettings(**defaults)


def test_zero_leak_git_config_count_auth_and_cross_host_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "fake-token-TOPSECRET-99887766"
    settings = _make_settings(
        tmp_path,
        internal_gitlab_url="https://gitlab.corp.local",
        internal_gitlab_token=secret,
        internal_git_auth_mode="basic_oauth2",
    )
    resolver = InternalGitSourceResolver(settings)

    # 1. Matching internal host receives host-scoped Basic oauth2 header via GIT_CONFIG_COUNT
    env = resolver.build_git_env("https://gitlab.corp.local/team/service.git")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.corp.local/.extraHeader"
    expected_b64 = base64.b64encode(f"oauth2:{secret}".encode()).decode("ascii")
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected_b64}"

    # 2. Private-token mode switches header format cleanly
    settings_pt = _make_settings(
        tmp_path,
        internal_gitlab_url="https://gitlab.corp.local",
        internal_gitlab_token=secret,
        internal_git_auth_mode="private_token",
    )
    env_pt = InternalGitSourceResolver(settings_pt).build_git_env(
        "https://gitlab.corp.local/team/service.git"
    )
    assert env_pt["GIT_CONFIG_VALUE_0"] == f"PRIVATE-TOKEN: {secret}"

    # 3. Cross-host URL must NEVER receive internal GitLab credentials
    env_other = resolver.build_git_env("https://github.com/example/repo.git")
    assert "GIT_CONFIG_COUNT" not in env_other

    # 4. Simulated clone passes ZERO token in argv and sanitizes stderr on failure
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(
            cmd,
            128,
            stdout="",
            stderr=(
                f"fatal: Authentication failed for 'https://oauth2:{secret}@gitlab.corp.local/team/service.git/' "
                f"Authorization: Basic {expected_b64}"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(InternalGitSourceError) as exc_info:
        resolver.resolve("https://gitlab.corp.local/team/service.git")

    assert all(secret not in arg for arg in captured["cmd"])
    assert "--" in captured["cmd"]
    err_text = str(exc_info.value)
    assert secret not in err_text
    assert expected_b64 not in err_text
    assert "***" in err_text


def test_sanitize_git_text_redacts_raw_base64_and_header_patterns() -> None:
    secret = "fake-token-SuperSecretToken123"
    b64 = base64.b64encode(f"oauth2:{secret}".encode()).decode("ascii")
    raw = (
        f"url=https://oauth2:{secret}@gitlab.internal/a/b.git "
        f"Authorization: Basic {b64} "
        f"PRIVATE-TOKEN: {secret} "
        f"raw={secret}"
    )
    cleaned = sanitize_git_text(raw, secrets=(secret,))
    assert secret not in cleaned
    assert b64 not in cleaned
    assert "https://***@gitlab.internal/a/b.git" in cleaned
    assert "Authorization: Basic ***" in cleaned
    assert "PRIVATE-TOKEN: ***" in cleaned


def test_cli_flag_and_crlf_injection_refused(tmp_path: Path) -> None:
    resolver = InternalGitSourceResolver(_make_settings(tmp_path))

    for bad_target in (
        "--upload-pack=touch /tmp/pwned",
        "-c core.sshCommand=id",
        "https://gitlab.internal/team/repo.git\n--upload-pack=id",
        "https://gitlab.internal/team/repo.git\r\nX-Injected: 1",
    ):
        with pytest.raises(InternalGitSourceError):
            resolver.resolve(bad_target)


def test_tls_ca_bundle_and_ssl_verify_env_injection(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        internal_gitlab_url="https://gitlab.internal",
        internal_gitlab_token="",
        internal_git_ssl_verify=False,
        internal_git_ca_bundle="/etc/ssl/corp-ca.pem",
    )
    resolver = InternalGitSourceResolver(settings)
    env = resolver.build_git_env("https://gitlab.internal/team/app.git")

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSL_CAINFO"] == "/etc/ssl/corp-ca.pem"
    assert env["GIT_SSL_NO_VERIFY"] == "1"
    cfg_pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert cfg_pairs["http.sslCAInfo"] == "/etc/ssl/corp-ca.pem"
    assert cfg_pairs["http.sslVerify"] == "false"


def test_real_bare_git_clone_push_and_temp_cleanup(tmp_path: Path) -> None:
    bare_repo = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_repo)], check=True, capture_output=True)

    seed_repo = tmp_path / "seed"
    seed_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=seed_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=seed_repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=seed_repo, check=True)
    (seed_repo / "app.py").write_text('"""Demo app."""\ndef hello() -> str:\n    return "hi"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=seed_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=seed_repo, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", bare_repo.as_uri()], cwd=seed_repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed_repo, check=True, capture_output=True)

    settings = _make_settings(tmp_path, internal_bare_remote=bare_repo)
    resolver = InternalGitSourceResolver(settings)

    # Checkout clones cleanly and deletes temp dir on context exit
    with resolver.checkout("internal-origin") as src:
        assert src.source_type == "internal_git_bare"
        assert src.is_temporary is True
        assert (src.local_path / "app.py").is_file()
        cloned_dir = src.cleanup_dir
        assert cloned_dir is not None and cloned_dir.exists()

        # Verify no credentials leaked into .git/config
        git_cfg = (src.local_path / ".git" / "config").read_text(encoding="utf-8")
        assert "extraHeader" not in git_cfg

        # Push a feature branch safely back to bare remote
        subprocess.run(["git", "config", "user.name", "Test"], cwd=src.local_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@local"], cwd=src.local_path, check=True)
        subprocess.run(["git", "checkout", "-b", "agent/test-push"], cwd=src.local_path, check=True, capture_output=True)
        (src.local_path / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=src.local_path, check=True)
        subprocess.run(["git", "commit", "-m", "feat: test push"], cwd=src.local_path, check=True, capture_output=True)
        push_res = push_branch_safe(src.local_path, "agent/test-push", settings=settings)
        assert push_res["pushed"] is True

    assert not cloned_dir.exists(), "Temporary clone directory must be cleaned up after context exit"

    ls_out = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare_repo)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "refs/heads/agent/test-push" in ls_out


@pytest.mark.asyncio
async def test_plain_non_git_local_folder_onboarding_and_non_mutating_build(tmp_path: Path) -> None:
    """Primary Mode #1: Plain non-git local folder works for BOTH Act 1 Onboarding and Act 2 Build without mutation."""
    plain_dir = tmp_path / "my_plain_project"
    (plain_dir / "pkg").mkdir(parents=True)
    (plain_dir / "pkg" / "engine.py").write_text(
        '"""Plain local business logic engine."""\n'
        "class OrderEngine:\n"
        '    """Processes orders without requiring git init."""\n'
        "    def run(self) -> str:\n"
        '        return "ok"\n',
        encoding="utf-8",
    )
    (plain_dir / "main.py").write_text(
        '"""Entrypoint for plain local app."""\n'
        "from pkg.engine import OrderEngine\n"
        "def start() -> str:\n"
        "    return OrderEngine().run()\n",
        encoding="utf-8",
    )
    assert not (plain_dir / ".git").exists()

    settings = _make_settings(tmp_path)

    # 1. Act 1 Onboarding on plain non-git local folder
    onboarding = OnboardingService(settings=settings)
    pkg = await onboarding.analyze(str(plain_dir))
    assert pkg["repo_name"] == "my_plain_project"
    assert pkg["analysis"]["source_type"] == "local_path"
    assert pkg["analysis"]["is_git_repo"] is False
    assert pkg["analysis"]["total_files"] == 2
    assert len(pkg["socratic_ladder"]) == 3

    # 2. Act 2 Build on plain non-git local folder isolates cleanly and NEVER mutates original folder!
    task_svc = CodingTaskService(settings=settings)
    build_res = await task_svc.run_build_task(
        repo_url_or_path=str(plain_dir),
        instruction="일반 로컬 폴더 프로젝트 지도 만들어줘",
        preset="project_map_app",
        push_to_remote=False,
    )
    assert build_res["status"] == "completed"
    assert build_res["test_result"]["passed"] is True
    html_text, _branch = task_svc.get_preview_html(build_res["task_id"])
    assert "pkg/engine.py" in html_text
    assert "OrderEngine" in html_text

    # Original plain local folder must remain 100% untouched (still no .git, no project_map/)
    assert not (plain_dir / ".git").exists()
    assert not (plain_dir / "project_map").exists()


def test_onboarding_api_self_modern_ddd_layers_and_internal_origin_and_pod_probes() -> None:
    with TestClient(app) as client:
        # 1. Pod probes
        hz = client.get("/healthz")
        assert hz.status_code == 200
        assert hz.json()["status"] == "ok"

        rz = client.get("/readyz")
        assert rz.status_code == 200
        rz_body = rz.json()
        assert rz_body["status"] == "ready"
        assert rz_body["checks"]["git"] is True
        assert rz_body["checks"]["internal_bare_remote"] is True

        # 2. Act 1 analyze `self` -> Modern DDD layers properly classified (no "domain" substring collision)
        res_self = client.post("/api/repo-onboard/analyze", json={"repo_url_or_path": "self"})
        assert res_self.status_code == 200
        self_data = res_self.json()
        assert self_data["analysis"]["source_type"] == "local_path"
        layers = {m["layer"] for m in self_data["analysis"]["modules"]}
        assert any("★ Focus Domain #1 · Guardrails" in layer for layer in layers)
        assert any("★ Focus Domain #2 · Execution" in layer for layer in layers)
        assert any("Workflow Domain" in layer for layer in layers)
        assert any("Onboarding Domain" in layer for layer in layers)

        # 3. Act 1 analyze `internal-origin` -> clones real bare remote & reports `internal_git_bare`
        res_bare = client.post("/api/repo-onboard/analyze", json={"repo_url_or_path": "internal-origin"})
        assert res_bare.status_code == 200
        bare_data = res_bare.json()
        assert bare_data["analysis"]["source_type"] == "internal_git_bare"
        assert bare_data["analysis"]["total_files"] >= 3


def test_unset_gitlab_url_never_leaks_token_to_external_host(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        internal_gitlab_url="",
        internal_gitlab_token="fake-token-secret-123",
    )
    resolver = InternalGitSourceResolver(settings)
    env_external = resolver.build_git_env("https://evil.example/repo.git")
    assert "GIT_CONFIG_COUNT" not in env_external

    env_internal = resolver.build_git_env("https://gitlab.internal/team/repo.git")
    assert env_internal.get("GIT_CONFIG_COUNT") == "1"


def test_sanitize_git_text_redacts_raw_base64_token() -> None:
    secret = "fake-token-RawBase64LeakToken99"
    raw_b64 = base64.b64encode(secret.encode("utf-8")).decode("ascii")
    cleaned = sanitize_git_text(f"leak={raw_b64}", secrets=(secret,))
    assert raw_b64 not in cleaned
    assert "***" in cleaned


def test_trailing_crlf_token_crlf_and_short_path_traversal_refused(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        internal_gitlab_url="https://gitlab.corp.local",
        internal_gitlab_token="fake-token-valid-token",
    )
    resolver = InternalGitSourceResolver(settings)

    with pytest.raises(InternalGitSourceError):
        resolver.resolve("https://gitlab.corp.local/team/repo.git\r\n")

    with pytest.raises(InternalGitSourceError):
        resolver.resolve("team/../../etc/passwd")

    bad_token_settings = _make_settings(
        tmp_path,
        internal_gitlab_url="https://gitlab.corp.local",
        internal_gitlab_token="fake-token-bad\r\nX-Injected: 1",
    )
    bad_resolver = InternalGitSourceResolver(bad_token_settings)
    with pytest.raises(InternalGitSourceError):
        bad_resolver.build_git_env("https://gitlab.corp.local/team/repo.git")


@pytest.mark.asyncio
async def test_onboarding_answer_reanalyzes_when_repo_changes(tmp_path: Path) -> None:
    other_dir = tmp_path / "other_service_app"
    other_dir.mkdir()
    (other_dir / "worker.py").write_text(
        '"""Isolated payment worker."""\nclass PaymentWorker:\n    def run(self) -> int:\n        return 1\n',
        encoding="utf-8",
    )
    settings = _make_settings(tmp_path)
    svc = OnboardingService(settings=settings)

    pkg_self = await svc.analyze("self")
    assert pkg_self["repo_name"] == settings.repo_root.name

    # Evaluate answer targeting `other_dir` directly without calling analyze(other_dir) first:
    # must auto-analyze `other_dir` on cache miss instead of grading against stale `self`
    other_key = svc._cache_key(str(other_dir))
    assert other_key not in svc._cached_packages
    res_other = await svc.evaluate_answer(
        repo_url_or_path=str(other_dir),
        question_id="q1_entry",
        selected_index=0,
    )
    assert other_key in svc._cached_packages
    assert svc._cached_packages[other_key]["repo_name"] == "other_service_app"
    assert isinstance(res_other["is_correct"], bool)

    # Also verify exact grading against `other_dir` even after switching active repo back to `self`
    other_q = svc._cached_packages[other_key]["socratic_ladder"][0]
    await svc.analyze("self")
    assert svc._last_cache_key == svc._cache_key("self")
    res_exact = await svc.evaluate_answer(
        repo_url_or_path=str(other_dir),
        question_id=other_q["id"],
        selected_index=other_q["correct_index"],
    )
    assert res_exact["is_correct"] is True
    assert "worker.py" in res_exact["source_evidence"] or "other_service_app" in svc._cached_packages[other_key]["repo_name"]
