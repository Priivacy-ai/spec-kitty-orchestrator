"""Git worktree preparation for orchestrator-owned execution."""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    """Raised when an orchestrator worktree cannot be prepared."""


def _run_git(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed in {repo_root}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def _is_git_worktree(path: Path) -> bool:
    return (path / ".git").exists()


def prepare_wp_worktree(host_repo_root: Path, workspace_path: Path, mission: str, wp_id: str) -> Path:
    """Create or reuse the WP worktree returned by the host API."""
    workspace_path = workspace_path if workspace_path.is_absolute() else host_repo_root / workspace_path
    branch = f"spec-kitty/orchestrator/{mission}/{wp_id}"

    if _is_git_worktree(workspace_path):
        return workspace_path
    if workspace_path.exists() and any(workspace_path.iterdir()):
        raise WorkspaceError(f"Cannot use non-empty non-worktree path: {workspace_path}")

    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(host_repo_root, ["worktree", "add", "-B", branch, str(workspace_path), "HEAD"])
    return workspace_path


__all__ = [
    "WorkspaceError",
    "prepare_wp_worktree",
]
