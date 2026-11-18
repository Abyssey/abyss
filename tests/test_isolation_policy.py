"""
Unit tests for [MY LEARNING CORE #2] abyss/core/isolation_policy.py
Verifies all 4 Claude Code Git Worktree Isolation Checks.
"""

from pathlib import Path
from abyss.core.isolation_policy import WorktreeIsolationPolicy


def test_check_1_file_edit_blocks_main_checkout_and_allows_worktree(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    wt_dir = main_repo / ".abyss" / "worktrees" / "worker-1"
    wt_dir.mkdir(parents=True)

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

    # 정상 평문 git 명령은 통과
    assert policy.validate_git_redirect("git status && git diff").allowed is True

    # git -C <main_repo> 차단
    v_git_c = policy.validate_git_redirect(f"git -C {main_repo} reset --hard")
    assert v_git_c.allowed is False
    assert v_git_c.check_name == "git_redirect"

    # --git-dir / GIT_DIR 환경변수 우회 차단
    assert policy.validate_git_redirect("git --git-dir=/tmp/.git status").allowed is False
    assert policy.validate_git_redirect("GIT_WORK_TREE=/tmp git checkout .").allowed is False

    # cd 로 Worktree 탈출 시도 차단
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
