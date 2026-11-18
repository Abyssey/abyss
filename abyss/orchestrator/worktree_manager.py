"""
abyss/orchestrator/worktree_manager.py
Claude Code Git Worktree Isolation & Merge Manager

- `git worktree add .abyss/worktrees/<agent_id> -b worktree-<agent_id>`로 서브에이전트별
  격리된 작업 디렉토리와 브랜치를 생성합니다.
- 실행 중에는 `git worktree lock`을 걸어 백그라운드 스위프나 외부 삭제를 방지합니다.
- `.worktreeinclude` 파일이 존재하면 `.env` 등 gitignore 대상 설정 파일을 Worktree로 복사합니다.
- 작업 완료 후 브랜치 커밋, `git diff main...worktree-<agent_id>` 추출, 그리고 Reviewer 승인 시
  `git merge --no-ff`를 수행합니다.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class WorktreeInfo:
    """개별 에이전트 Worktree 메타데이터."""

    agent_id: str
    branch_name: str
    worktree_path: Path
    base_branch: str
    locked: bool = False
    head_commit: str = ""
    status: str = "active"  # "active" | "committed" | "merged" | "conflict" | "cleaned"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "branch_name": self.branch_name,
            "worktree_path": str(self.worktree_path),
            "base_branch": self.base_branch,
            "locked": self.locked,
            "head_commit": self.head_commit,
            "status": self.status,
        }


class GitWorktreeManager:
    """
    Claude Code `Run parallel sessions with worktrees` 규격을 구현한
    Git Worktree 생명주기 관리자.
    """

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktrees_dir = self.repo_root / ".abyss" / "worktrees"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees: dict[str, WorktreeInfo] = {}
        self._ensure_git_repo()

    def _run_git(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        target_cwd = cwd or self.repo_root
        return subprocess.run(
            ["git", *args],
            cwd=str(target_cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    def _ensure_git_repo(self) -> None:
        """대상 워크스페이스가 유효한 Git 저장소이고 초기 커밋이 있는지 확인합니다."""
        if not (self.repo_root / ".git").exists():
            self._run_git(["init", "-b", "main"])
        # git user 설정 확인
        name_res = self._run_git(["config", "user.name"])
        if not name_res.stdout.strip():
            self._run_git(["config", "user.name", "Abyss Coding Harness"])
            self._run_git(["config", "user.email", "abyss@local.harness"])

        head_res = self._run_git(["rev-parse", "--verify", "HEAD"])
        if head_res.returncode != 0:
            readme = self.repo_root / "README.md"
            if not readme.exists():
                readme.write_text("# Target Workspace Repository\n", encoding="utf-8")
            self._run_git(["add", "."])
            self._run_git(["commit", "-m", "chore: initialize base repository for worktrees"])

    def get_default_branch(self) -> str:
        res = self._run_git(["symbolic-ref", "--short", "HEAD"])
        return res.stdout.strip() or "main"

    def _apply_worktreeinclude(self, wt_path: Path) -> list[str]:
        """
        Claude Code `.worktreeinclude` 규격:
        메인 체크아웃의 `.worktreeinclude`에 명시된 패턴 중 gitignore된 파일(.env 등)을
        새로 생성된 Worktree에 자동 복사합니다.
        """
        include_file = self.repo_root / ".worktreeinclude"
        if not include_file.exists():
            return []

        patterns = [
            line.strip()
            for line in include_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        copied: list[str] = []
        for candidate in self.repo_root.iterdir():
            if candidate.name in (".git", ".abyss"):
                continue
            for pat in patterns:
                if fnmatch.fnmatch(candidate.name, pat) and candidate.is_file():
                    dest = wt_path / candidate.name
                    shutil.copy2(candidate, dest)
                    copied.append(candidate.name)
        return copied

    def create_worktree(self, agent_id: str, base_branch: str | None = None) -> WorktreeInfo:
        """
        에이전트 전용 Git Worktree(`.abyss/worktrees/<agent_id>`)와
        전용 브랜치(`worktree-<agent_id>`)를 생성하고 Lock을 설정합니다.
        """
        base = base_branch or self.get_default_branch()
        branch_name = f"worktree-{agent_id}"
        wt_path = self.worktrees_dir / agent_id

        # 기존 동일 이름의 Worktree가 남아 있으면 먼저 정리
        if wt_path.exists():
            self._run_git(["worktree", "unlock", str(wt_path)])
            self._run_git(["worktree", "remove", "--force", str(wt_path)])
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
        self._run_git(["worktree", "prune"])
        self._run_git(["branch", "-D", branch_name])

        # 새 Worktree 및 브랜치 생성
        res = self._run_git(["worktree", "add", "-b", branch_name, str(wt_path), base])
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree for {agent_id}: {res.stderr}")

        # Claude Code 규격: 실행 중에는 git worktree lock 유지
        lock_res = self._run_git(
            ["worktree", "lock", "--reason", f"Active Abyss subagent {agent_id}", str(wt_path)]
        )
        locked = lock_res.returncode == 0

        self._apply_worktreeinclude(wt_path)
        head_commit = self._run_git(["rev-parse", "--short", "HEAD"], cwd=wt_path).stdout.strip()

        info = WorktreeInfo(
            agent_id=agent_id,
            branch_name=branch_name,
            worktree_path=wt_path,
            base_branch=base,
            locked=locked,
            head_commit=head_commit,
            status="active",
        )
        self.worktrees[agent_id] = info
        return info

    def commit_worktree_changes(self, agent_id: str, commit_message: str) -> str:
        """Worktree 내의 변경 사항을 해당 에이전트 브랜치에 커밋합니다."""
        info = self.worktrees[agent_id]
        self._run_git(["add", "-A"], cwd=info.worktree_path)
        status_res = self._run_git(["status", "--porcelain"], cwd=info.worktree_path)
        if status_res.stdout.strip():
            self._run_git(["commit", "-m", commit_message], cwd=info.worktree_path)
        head_commit = self._run_git(
            ["rev-parse", "--short", "HEAD"], cwd=info.worktree_path
        ).stdout.strip()
        info.head_commit = head_commit
        info.status = "committed"
        return head_commit

    def get_worktree_diff(self, agent_id: str) -> str:
        """베이스 브랜치 대비 현재 Worktree 브랜치의 `git diff`를 반환합니다."""
        info = self.worktrees.get(agent_id)
        if not info:
            return ""
        diff_res = self._run_git(["diff", f"{info.base_branch}...{info.branch_name}"])
        if not diff_res.stdout.strip():
            # 아직 커밋 전인 워킹 트리 변경분 조회
            diff_res = self._run_git(["diff", "HEAD"], cwd=info.worktree_path)
        return diff_res.stdout

    def merge_worktree_branch(self, agent_id: str) -> dict[str, Any]:
        """
        Reviewer 에이전트가 승인한 Worktree 브랜치를 베이스 브랜치(main)로 머지합니다.
        충돌 발생 시 자동으로 `git merge --abort` 후 충돌 내역을 반환하여 Worker가 재작업하도록 합니다.
        """
        info = self.worktrees[agent_id]
        merge_res = self._run_git(
            [
                "merge",
                "--no-ff",
                info.branch_name,
                "-m",
                f"Merge branch '{info.branch_name}' (approved by Reviewer Agent)",
            ]
        )
        if merge_res.returncode == 0:
            info.status = "merged"
            new_head = self._run_git(["rev-parse", "--short", "HEAD"]).stdout.strip()
            return {
                "merged": True,
                "branch": info.branch_name,
                "main_head": new_head,
                "output": merge_res.stdout.strip(),
            }

        # 머지 충돌 발생 시
        conflict_files = self._run_git(["diff", "--name-only", "--diff-filter=U"]).stdout.splitlines()
        self._run_git(["merge", "--abort"])
        info.status = "conflict"
        return {
            "merged": False,
            "branch": info.branch_name,
            "conflict_files": conflict_files,
            "error": (merge_res.stderr or merge_res.stdout).strip(),
        }

    def unlock_and_cleanup(self, agent_id: str, remove_branch: bool = False) -> None:
        """작업 및 머지가 끝난 Worktree의 Lock을 해제하고 디렉토리를 정리합니다."""
        info = self.worktrees.get(agent_id)
        if not info:
            return
        self._run_git(["worktree", "unlock", str(info.worktree_path)])
        info.locked = False
        self._run_git(["worktree", "remove", "--force", str(info.worktree_path)])
        if remove_branch:
            self._run_git(["branch", "-D", info.branch_name])
        info.status = "cleaned"

    def get_git_graph_log(self, limit: int = 15) -> list[dict[str, str]]:
        """시각화를 위해 최근 Git 브랜치/머지 히스토리를 조회합니다."""
        res = self._run_git(
            ["log", "--all", "--graph", "--oneline", "--decorate", f"-n{limit}"]
        )
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        return [{"raw": line} for line in lines]
