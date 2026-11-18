"""
abyss/core/tools.py
Worktree-isolated Coding & Team Collaboration Tools for Abyss Agents.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable


def build_agent_tools(
    worktree_root: str | Path,
    agent_id: str,
    send_mailbox_fn: Callable[[str, str, str], str] | None = None,
    read_mailbox_fn: Callable[[str], str] | None = None,
    update_task_fn: Callable[[str, str, str], str] | None = None,
) -> tuple[dict[str, Callable[..., str]], list[dict[str, Any]]]:
    """
    지정된 `worktree_root` 내부에서 동작하는 코딩 도구 7종과
    OpenAI Function Calling 호환 JSON Schema를 생성하여 반환합니다.
    """
    wt_root = Path(worktree_root).resolve()

    def _resolve_in_wt(rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return (wt_root / p).resolve() if not p.is_absolute() else p.resolve()

    def list_files(directory: str = ".") -> str:
        target = _resolve_in_wt(directory)
        if not target.exists():
            return f"Directory '{directory}' does not exist."
        items: list[str] = []
        for entry in sorted(target.rglob("*")):
            if ".git" in entry.parts or "__pycache__" in entry.parts:
                continue
            rel = entry.relative_to(wt_root)
            prefix = "[DIR] " if entry.is_dir() else "[FILE]"
            items.append(f"{prefix} {rel}")
        return "\n".join(items[:200]) if items else "(empty directory)"

    def read_file(path: str) -> str:
        target = _resolve_in_wt(path)
        if not target.exists() or not target.is_file():
            return f"File '{path}' not found."
        content = target.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        numbered = [f"{idx + 1:4d}: {line}" for idx, line in enumerate(lines[:400])]
        return "\n".join(numbered)

    def write_file(path: str, content: str) -> str:
        target = _resolve_in_wt(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} bytes to '{target.relative_to(wt_root)}'."

    def edit_file(path: str, old_text: str, new_text: str) -> str:
        target = _resolve_in_wt(path)
        if not target.exists():
            return f"Error: File '{path}' does not exist."
        text = target.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: Target snippet not found in '{path}'."
        updated = text.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")
        return f"Updated '{target.relative_to(wt_root)}' successfully."

    def run_command(command: str, cwd: str | None = None) -> str:
        import os
        import sys

        exec_cwd = _resolve_in_wt(cwd) if cwd else wt_root
        env = os.environ.copy()
        env["PYTHONPATH"] = str(exec_cwd)
        venv_bin = str(Path(sys.executable).parent)
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(exec_cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return f"[exit_code={proc.returncode}]\n{out.strip()[:2000]}"

    def send_team_message(recipient: str, message: str) -> str:
        if send_mailbox_fn is None:
            return "Mailbox not configured."
        return send_mailbox_fn(agent_id, recipient, message)

    def read_team_messages() -> str:
        if read_mailbox_fn is None:
            return "No messages."
        return read_mailbox_fn(agent_id)

    def update_task_status(task_id: str, status: str, note: str = "") -> str:
        if update_task_fn is None:
            return f"Task {task_id} -> {status}"
        return update_task_fn(task_id, status, note)

    registry: dict[str, Callable[..., str]] = {
        "list_files": list_files,
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "run_command": run_command,
        "send_team_message": send_team_message,
        "read_team_messages": read_team_messages,
        "update_task_status": update_task_status,
    }

    schemas: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files inside the isolated git worktree.",
                "parameters": {
                    "type": "object",
                    "properties": {"directory": {"type": "string", "default": "."}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file inside the isolated git worktree with line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or overwrite a file inside the isolated git worktree.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace old_text with new_text in a file inside the isolated git worktree.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Run a shell/pytest/git command inside the isolated git worktree.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_team_message",
                "description": "Send an asynchronous message (e.g., JSON schema contract) to a teammate or lead via Agent Teams Mailbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "recipient": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["recipient", "message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_team_messages",
                "description": "Read incoming messages from teammates or lead in your Agent Teams Mailbox.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_task_status",
                "description": "Update task status on the Shared Task Board (pending, in_progress, in_review).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "status": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["task_id", "status"],
                },
            },
        },
    ]

    return registry, schemas
