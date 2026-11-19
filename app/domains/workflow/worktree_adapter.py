"""
app/domains/workflow/worktree_adapter.py
Git Worktree Isolation, Internal Bare Remote Fixture & Safe Branch Push Adapter.

- `git worktree add .abyss/worktrees/<agent_id> -b <branch_name>`로 서브에이전트/태스크별
  격리된 작업 디렉토리와 브랜치를 생성하고 `git worktree lock`을 유지합니다.
- `.worktreeinclude`에 명시된 로컬 설정 파일을 새 Worktree로 자동 복사합니다.
- 오프라인/테스트 환경에서 사내 원격 푸시·머지를 검증할 수 있도록
  `ensure_internal_bare_remote()` 및 zero-leak `push_branch()`를 제공합니다.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import AppSettings
from app.core.git_source import push_branch_safe, sanitize_git_text


@dataclass
class WorktreeInfo:
    """개별 에이전트/태스크 Worktree 메타데이터."""

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
    """Git Worktree 생명주기, 로컬/원격 푸시 및 브랜치 머지 관리자."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktrees_dir = self.repo_root / ".abyss" / "worktrees"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees: dict[str, WorktreeInfo] = {}
        self.internal_remote_url: str = ""
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
        name_res = self._run_git(["config", "user.name"])
        if not name_res.stdout.strip():
            self._run_git(["config", "user.name", "Coding Agent Harness"])
            self._run_git(["config", "user.email", "agent@local.harness"])

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

    def ensure_internal_bare_remote(self, bare_repo_path: str | Path) -> str:
        """
        오프라인/단위 테스트용 로컬 Bare Git 원격 저장소를 멱등적으로 초기화하고
        `origin` 리모트(`file:///...`)를 연결한 뒤 기본 브랜치(`main`)를 푸시합니다.
        """
        bare_path = Path(bare_repo_path).resolve()
        if not (bare_path / "HEAD").exists():
            bare_path.mkdir(parents=True, exist_ok=True)
            init_res = subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(bare_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if init_res.returncode != 0:
                subprocess.run(
                    ["git", "init", "--bare", str(bare_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(bare_path), "symbolic-ref", "HEAD", "refs/heads/main"],
                    capture_output=True,
                    text=True,
                    check=False,
                )

        bare_uri = bare_path.as_uri()
        existing = self._run_git(["remote", "get-url", "origin"])
        if existing.returncode != 0 or not existing.stdout.strip():
            self._run_git(["remote", "add", "origin", bare_uri])
        elif existing.stdout.strip() != bare_uri:
            self._run_git(["remote", "set-url", "origin", bare_uri])

        default_branch = self.get_default_branch()
        self._run_git(["push", "-u", "origin", default_branch])
        self.internal_remote_url = bare_uri
        return bare_uri

    def get_remote_info(self, remote: str = "origin") -> dict[str, Any]:
        res = self._run_git(["remote", "get-url", remote])
        raw_url = res.stdout.strip() if res.returncode == 0 else ""
        clean_url = sanitize_git_text(raw_url)
        return {
            "configured": bool(clean_url),
            "remote_name": remote,
            "url": clean_url,
        }

    def push_branch(
        self,
        branch: str,
        *,
        remote: str = "origin",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """지정된 브랜치를 원격 저장소로 zero-leak 환경변수를 통해 안전하게 푸시합니다."""
        return push_branch_safe(
            self.repo_root,
            branch,
            remote=remote,
            settings=settings,
        )

    def _apply_worktreeinclude(self, wt_path: Path) -> list[str]:
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

    def create_worktree(
        self,
        agent_id: str,
        base_branch: str | None = None,
        branch_name: str | None = None,
    ) -> WorktreeInfo:
        """
        에이전트/태스크 전용 Git Worktree(`.abyss/worktrees/<agent_id>`)와
        전용 브랜치(기본 `worktree-<agent_id>`, 지정 시 예: `agent/task-<id>`)를 생성하고 Lock을 설정합니다.
        """
        base = base_branch or self.get_default_branch()
        target_branch = (branch_name or f"worktree-{agent_id}").strip()
        if target_branch.startswith("-") or any(ch in target_branch for ch in ("\r", "\n", "\x00")):
            raise ValueError(f"Invalid branch name: {target_branch!r}")

        wt_path = self.worktrees_dir / agent_id

        if wt_path.exists():
            self._run_git(["worktree", "unlock", str(wt_path)])
            self._run_git(["worktree", "remove", "--force", str(wt_path)])
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
        self._run_git(["worktree", "prune"])
        self._run_git(["branch", "-D", target_branch])

        res = self._run_git(["worktree", "add", "-b", target_branch, str(wt_path), base])
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree for {agent_id}: {res.stderr}")

        lock_res = self._run_git(
            ["worktree", "lock", "--reason", f"Active coding subagent {agent_id}", str(wt_path)]
        )
        locked = lock_res.returncode == 0

        self._apply_worktreeinclude(wt_path)
        head_commit = self._run_git(["rev-parse", "--short", "HEAD"], cwd=wt_path).stdout.strip()

        info = WorktreeInfo(
            agent_id=agent_id,
            branch_name=target_branch,
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
            diff_res = self._run_git(["diff", "HEAD"], cwd=info.worktree_path)
        return diff_res.stdout

    def get_branch_diff(self, base_branch: str, branch_name: str) -> str:
        """임의의 두 브랜치(`base_branch...branch_name`) 사이의 unified diff를 반환합니다."""
        res = self._run_git(["diff", f"{base_branch}...{branch_name}"])
        return res.stdout

    def get_branch_changed_files(self, base_branch: str, branch_name: str) -> list[str]:
        """베이스 브랜치 대비 대상 브랜치에서 변경된 파일 상대 경로 목록을 반환합니다."""
        res = self._run_git(["diff", "--name-only", f"{base_branch}...{branch_name}"])
        return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]

    def merge_worktree_branch(self, agent_id: str) -> dict[str, Any]:
        """Reviewer가 승인한 Worktree 브랜치를 베이스 브랜치(main)로 `--no-ff` 머지합니다."""
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
