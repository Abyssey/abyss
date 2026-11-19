"""
Unit tests for Focus Domain #1 (`app/domains/guardrails/isolation_policy.py`).
Verifies all 4 deterministic Git Worktree Isolation Checks, including symlink & command substitution escapes.
"""

from __future__ import annotations

from pathlib import Path

from app.domains.guardrails.isolation_policy import WorktreeIsolationPolicy


def test_check_1_file_edit_blocks_main_checkout_symlink_and_allows_worktree(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    wt_dir.mkdir(parents=True)
    secret_file = main_repo / ".env"
    secret_file.write_text("SECRET=1\n", encoding="utf-8")

    policy = WorktreeIsolationPolicy(worktree_root=wt_dir, main_checkout_root=main_repo)

    # 1. Worktree 내부 파일 수정은 허용
    ok_verdict = policy.validate_file_edit("src/feature.py")
    assert ok_verdict.allowed is True
    assert ok_verdict.check_name == "file_edit"

    # 2. 상대 경로 탈출(../../../main.py)을 통한 Main checkout 수정은 차단
    escape_verdict = policy.validate_file_edit("../../../main.py")
    assert escape_verdict.allowed is False
    assert "main checkout" in escape_verdict.reason.lower()

    # 3. 절대 경로로 Main checkout 직접 수정 시도 차단
    abs_verdict = policy.validate_file_edit(main_repo / "README.md")
    assert abs_verdict.allowed is False

    # 4. Worktree의 .git identity 포인터 파일 직접 덮어쓰기 차단
    git_id_verdict = policy.validate_file_edit(wt_dir / ".git")
    assert git_id_verdict.allowed is False

    # 5. 심볼릭 링크를 통한 Main checkout 탈출 차단
    symlink_escape = wt_dir / "link_to_env"
    symlink_escape.symlink_to(secret_file)
    symlink_verdict = policy.validate_file_edit("link_to_env")
    assert symlink_verdict.allowed is False
    assert symlink_verdict.check_name == "file_edit"


def test_check_2_command_cwd_enforcement(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    wt_dir.mkdir(parents=True)

    policy = WorktreeIsolationPolicy(worktree_root=wt_dir, main_checkout_root=main_repo)

    assert policy.validate_command_cwd(None).allowed is True
    assert policy.validate_command_cwd(wt_dir / "sub").allowed is True
    assert policy.validate_command_cwd(main_repo).allowed is False
    assert policy.validate_command_cwd("/tmp").allowed is False


def test_check_3_git_redirects_and_cd_escape_blocked(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    wt_dir.mkdir(parents=True)

    policy = WorktreeIsolationPolicy(worktree_root=wt_dir, main_checkout_root=main_repo)

    assert policy.validate_git_redirect("git status && git diff").allowed is True

    v_git_c = policy.validate_git_redirect(f"git -C {main_repo} reset --hard")
    assert v_git_c.allowed is False
    assert v_git_c.check_name == "git_redirect"

    assert policy.validate_git_redirect("git --git-dir=/tmp/.git status").allowed is False
    assert policy.validate_git_redirect("GIT_WORK_TREE=/tmp git checkout .").allowed is False
    assert policy.validate_git_redirect("cd ../../../ && git status").allowed is False


def test_check_4_command_shape_blocks_dynamic_expansion(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    wt_dir.mkdir(parents=True)

    policy = WorktreeIsolationPolicy(worktree_root=wt_dir, main_checkout_root=main_repo)

    assert policy.validate_command_shape("pytest tests/ -q").allowed is True
    assert policy.validate_command_shape("eval $DANGEROUS_CMD").allowed is False
    assert policy.validate_command_shape("${!indirect_var} status").allowed is False
    assert policy.validate_command_shape("git `cat target_cmd`").allowed is False
    assert policy.validate_command_shape("git $(echo status)").allowed is False


def test_check_1_blocks_git_directory_internals_config_and_hooks(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    (wt_dir / ".git" / "hooks").mkdir(parents=True)

    policy = WorktreeIsolationPolicy(worktree_root=wt_dir, main_checkout_root=main_repo)

    for rel in (".git/config", ".git/hooks/pre-commit"):
        verdict = policy.validate_file_edit(rel)
        assert verdict.allowed is False
        assert verdict.check_name == "file_edit"


def test_check_3_blocks_multi_segment_global_flag_attached_c_and_cd_pushd_escapes(
    tmp_path: Path,
) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    wt_dir.mkdir(parents=True)

    policy = WorktreeIsolationPolicy(worktree_root=wt_dir, main_checkout_root=main_repo)

    blocked_cmds = [
        f"git -C . status && git -C {main_repo} reset --hard",
        f"git --no-pager -C {main_repo} status",
        f"git -C{main_repo} status",
        "cd ~/repo && git status",
        "cd && pwd",
        "cd -- ../ && git status",
        "cd $HOME && pwd",
        "pushd ../ && pwd",
    ]
    for cmd in blocked_cmds:
        verdict = policy.validate_git_redirect(cmd)
        assert verdict.allowed is False, f"Expected Check #3 block for: {cmd!r}"
        assert verdict.check_name == "git_redirect"


def test_check_4_blocks_general_command_and_process_substitution(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    wt_dir.mkdir(parents=True)

    policy = WorktreeIsolationPolicy(worktree_root=wt_dir, main_checkout_root=main_repo)

    blocked_cmds = [
        "echo $(id)",
        "cd $(echo /etc) && cat passwd",
        "$(echo git) status",
        'python -m app --arg "x $(touch /tmp/x)"',
        "cat <(cat ../../.env)",
    ]
    for cmd in blocked_cmds:
        verdict = policy.validate_command_shape(cmd)
        assert verdict.allowed is False, f"Expected Check #4 block for: {cmd!r}"
        assert verdict.check_name == "command_shape"


def test_permission_rules_blocks_quoted_env_escapes_wrappers_and_normalized_paths() -> None:
    from app.domains.guardrails.permissions import PermissionRules

    rules = PermissionRules.default()
    denied_bash_cmds = [
        'cd \'app\' && FOO=1 BAR="a && b" git push origin main',
        'FOO="hello world" git push origin main',
        'echo "\\"" && git push origin main',
        'echo \\" && git push origin main',
        "(git push origin main)",
        "{ git push origin main; }",
        "env git push origin main",
        "command git push origin main",
        "/usr/bin/git push origin main",
        "\\git push origin main",
        "'git' push origin main",
        "git  push origin main",
        "rm -fr /",
        "rm -r -f /",
    ]
    for cmd in denied_bash_cmds:
        verdict = rules.evaluate("Bash", cmd)
        assert verdict.decision == "deny", f"Expected deny for {cmd!r}, got {verdict}"

    assert rules.evaluate("Read", "sub/../.env").decision == "deny"
    assert rules.evaluate("Write", "sub/../.env").decision == "deny"
