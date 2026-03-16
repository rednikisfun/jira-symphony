"""Git worktree management for isolated Claude Code sessions."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from .models import ProjectConfig

log = logging.getLogger(__name__)

WORKTREE_DIR = ".worktrees/symphony"


async def _git(*args: str, cwd: str | Path) -> tuple[int, str, str]:
    """Run a git command safely using exec (no shell). Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def create_worktree(
    project: ProjectConfig, issue_key: str
) -> tuple[str, str]:
    """Create a git worktree for the issue.

    Returns (worktree_path, branch_name).
    """
    branch = f"symphony/{issue_key}"
    worktree_path = str(Path(project.path) / WORKTREE_DIR / issue_key)

    # Fetch latest from origin
    log.info("Fetching origin in %s", project.path)
    rc, _, err = await _git("fetch", "origin", cwd=project.path)
    if rc != 0:
        log.warning("git fetch failed: %s", err)

    # Remove stale worktree if exists
    wt_dir = Path(worktree_path)
    if wt_dir.exists():
        log.info("Removing stale worktree: %s", worktree_path)
        await _git("worktree", "remove", "--force", worktree_path, cwd=project.path)
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)

    # Delete branch if it exists locally (leftover from previous attempt)
    await _git("branch", "-D", branch, cwd=project.path)

    # Create worktree
    log.info("Creating worktree: %s (branch: %s)", worktree_path, branch)
    rc, out, err = await _git(
        "worktree", "add",
        worktree_path,
        "-b", branch,
        f"origin/{project.main_branch}",
        cwd=project.path,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to create worktree: {err}")

    return worktree_path, branch


async def cleanup_worktree(project_path: str, worktree_path: str) -> None:
    """Remove a worktree and its directory."""
    log.info("Cleaning up worktree: %s", worktree_path)
    await _git("worktree", "remove", "--force", worktree_path, cwd=project_path)
    wt = Path(worktree_path)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)


async def push_branch(project_path: str, branch: str) -> bool:
    """Push branch to origin. Returns True on success."""
    log.info("Pushing branch %s", branch)
    rc, _, err = await _git("push", "origin", branch, cwd=project_path)
    if rc != 0:
        log.error("Push failed: %s", err)
        return False
    return True
