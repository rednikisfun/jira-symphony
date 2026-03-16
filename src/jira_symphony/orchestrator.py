"""Main orchestrator — tick-based scheduler loop with controls."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .claude_worker import ClaudeWorker
from .config import SymphonyConfig
from .git_provider import GitProvider, get_git_provider
from .jira_client import JiraClient
from .models import JiraIssue, Worker, WorkerStatus
from .prompt_renderer import PromptRenderer
from .router import Router
from .state import StateStore
from .workspace import cleanup_worktree, create_worktree, push_branch

log = logging.getLogger(__name__)


class Orchestrator:
    """Main loop: poll Jira -> route -> dispatch -> handle results."""

    def __init__(self, config: SymphonyConfig) -> None:
        self.config = config
        self.jira = JiraClient(config)
        self.router = Router(config)
        self.renderer = PromptRenderer()
        self.state = StateStore()

        # Active Claude workers: issue_key -> ClaudeWorker
        self._workers: dict[str, ClaudeWorker] = {}
        # Cached git provider instances: provider_name -> GitProvider
        self._git_providers: dict[str, GitProvider] = {}
        self._running = False
        self._paused = False
        self._last_poll_at: datetime | None = None
        self._total_dispatched = 0
        self._total_completed = 0
        self._total_failed = 0

    def _get_git_provider(self, provider_name: str) -> GitProvider:
        """Get or create a GitProvider for the given type."""
        if provider_name not in self._git_providers:
            if provider_name == "github":
                token = self.config.github.token
            elif provider_name == "gitlab":
                token = self.config.gitlab.token
            else:
                raise ValueError(f"Unknown git provider: {provider_name}")
            self._git_providers[provider_name] = get_git_provider(
                provider_name,
                token,
                self.config.gitlab.base_url if provider_name == "gitlab" else "",
            )
        return self._git_providers[provider_name]

    # ── Controls ──────────────────────────────────────────────

    async def start(self) -> None:
        await self.state.init()
        self._running = True
        await self._recover_stale_workers()

        log.info(
            "Symphony orchestrator started. Polling every %ds, max %d workers.",
            self.config.jira.poll_interval_seconds,
            self.config.claude.max_workers,
        )

        while self._running:
            try:
                if not self._paused:
                    await self._tick()
            except Exception:
                log.exception("Error in orchestrator tick")
            await asyncio.sleep(self.config.jira.poll_interval_seconds)

    async def stop(self) -> None:
        self._running = False
        log.info("Shutting down — killing %d active workers", len(self._workers))
        for cw in self._workers.values():
            cw.kill()
        await self.jira.close()
        for gp in self._git_providers.values():
            await gp.close()
        await self.state.close()

    def pause(self) -> None:
        self._paused = True
        log.info("Orchestrator paused — no new dispatches")

    def resume(self) -> None:
        self._paused = False
        log.info("Orchestrator resumed")

    async def kill_worker(self, issue_key: str) -> bool:
        """Kill a specific worker. Returns True if found."""
        cw = self._workers.get(issue_key)
        if not cw:
            return False
        cw.kill()
        cw.worker.status = WorkerStatus.FAILED
        cw.worker.error = "Manually killed via dashboard"
        cw.worker.finished_at = datetime.now()
        cw.progress.current_activity = "killed"
        await self.state.upsert_worker(cw.worker)
        log.info("%s: killed by user", issue_key)
        return True

    async def retry_worker(self, issue_key: str) -> bool:
        """Retry a failed/completed worker. Returns True if triggered."""
        existing = await self.state.get_worker(issue_key)
        if not existing:
            return False
        # Transition back to To Do so the next tick picks it up
        try:
            await self.jira.transition_issue(
                issue_key, self.config.jira.transitions.to_do
            )
        except Exception:
            log.exception("Failed to transition %s back to To Do", issue_key)
        return True

    async def manual_dispatch(self, issue_key: str, project_key: str | None = None) -> str:
        """Queue an issue for manual dispatch. Returns status message."""
        if issue_key in self._workers:
            return f"{issue_key} is already being processed"
        await self.state.enqueue_manual(issue_key, project_key)
        log.info("Manual dispatch queued: %s (project: %s)", issue_key, project_key or "auto")
        return f"{issue_key} queued for dispatch"

    # ── Status ────────────────────────────────────────────────

    def get_status(self) -> dict:
        workers = []
        for key, cw in self._workers.items():
            w = cw.worker
            elapsed = ""
            if w.started_at:
                delta = datetime.now() - w.started_at
                mins, secs = divmod(int(delta.total_seconds()), 60)
                elapsed = f"{mins}m {secs}s" if mins else f"{secs}s"
            workers.append({
                "issue": w.issue_key,
                "project": w.project_key,
                "status": w.status.value,
                "attempt": w.attempt,
                "elapsed": elapsed,
                "pid": w.pid,
                "session_id": w.session_id,
                "branch": w.branch_name,
                "worktree": w.worktree_path,
                "progress": cw.progress.to_dict(),
            })
        return {
            "running": self._running,
            "paused": self._paused,
            "active_workers": len(self._workers),
            "max_workers": self.config.claude.max_workers,
            "total_dispatched": self._total_dispatched,
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
            "last_poll_at": self._last_poll_at.isoformat() if self._last_poll_at else None,
            "poll_interval": self.config.jira.poll_interval_seconds,
            "workers": workers,
            "projects": [
                {"name": p.name, "description": p.description, "path": p.path}
                for p in self.config.projects
            ],
        }

    async def get_history(self) -> list[dict]:
        """Get all workers from DB for history view."""
        all_workers = await self.state.get_all_workers()
        return [
            {
                "issue": w.issue_key,
                "project": w.project_key,
                "status": w.status.value,
                "attempt": w.attempt,
                "started_at": w.started_at.isoformat() if w.started_at else None,
                "finished_at": w.finished_at.isoformat() if w.finished_at else None,
                "session_id": w.session_id,
                "output": w.output[:500] if w.output else "",
                "error": w.error[:500] if w.error else "",
            }
            for w in all_workers
        ]

    # ── Core loop ─────────────────────────────────────────────

    async def _tick(self) -> None:
        await self._reconcile_workers()
        await self._process_manual_queue()

        issues = await self.jira.poll_todo_issues()
        self._last_poll_at = datetime.now()

        active_keys = set(self._workers.keys())
        new_issues = [i for i in issues if i.key not in active_keys]

        if new_issues:
            log.info("New issues to process: %s", [i.key for i in new_issues])

        available_slots = self.config.claude.max_workers - len(self._workers)
        for issue in new_issues[:available_slots]:
            await self._dispatch(issue)

    async def _process_manual_queue(self) -> None:
        """Process manually dispatched issues."""
        items = await self.state.dequeue_manual()
        for issue_key, project_key in items:
            if issue_key in self._workers:
                log.info("Skipping manual dispatch %s — already active", issue_key)
                continue
            if len(self._workers) >= self.config.claude.max_workers:
                log.warning("Manual dispatch %s — no slots, re-queuing", issue_key)
                await self.state.enqueue_manual(issue_key, project_key)
                break
            try:
                issue = await self.jira.get_issue(issue_key)
                await self._dispatch(issue, override_project=project_key)
            except Exception:
                log.exception("Manual dispatch failed for %s", issue_key)

    async def _reconcile_workers(self) -> None:
        completed: list[str] = []

        for key, cw in self._workers.items():
            if cw.is_running:
                continue
            completed.append(key)
            w = cw.worker

            if w.status == WorkerStatus.COMPLETED:
                self._total_completed += 1
                await self._handle_completion(w)
            elif w.status in (WorkerStatus.FAILED, WorkerStatus.TIMED_OUT):
                self._total_failed += 1
                await self._handle_failure(w)

        for key in completed:
            del self._workers[key]

    async def _dispatch(
        self, issue: JiraIssue, override_project: str | None = None
    ) -> None:
        if override_project:
            project_key = override_project
        else:
            project_key = await self.router.route_with_triage(issue)
        if not project_key:
            log.warning("Skipping %s: no route found", issue.key)
            return

        project = self.config.get_project(project_key)
        if not project:
            log.error("Unknown project key: %s", project_key)
            return

        try:
            wt_path, branch = await create_worktree(project, issue.key)

            worker = Worker(
                issue_key=issue.key,
                project_key=project_key,
                worktree_path=wt_path,
                branch_name=branch,
            )

            prompt = self.renderer.render(issue)

            await self.jira.transition_issue(issue.key, project.transition_id)

            cw = ClaudeWorker(worker, project, self.config.claude, prompt)
            await cw.start()
            await self.state.upsert_worker(worker)

            self._workers[issue.key] = cw
            self._total_dispatched += 1
            asyncio.create_task(self._monitor_worker(cw))

        except Exception:
            log.exception("Failed to dispatch %s", issue.key)

    async def _monitor_worker(self, cw: ClaudeWorker) -> None:
        """Stream and wait for a worker to finish."""
        timeout = self.config.claude.timeout_minutes * 60
        await cw.stream_and_wait(timeout)
        await self.state.upsert_worker(cw.worker)

    async def _handle_completion(self, w: Worker) -> None:
        log.info("%s: handling completion", w.issue_key)

        project = self.config.get_project(w.project_key)
        if not project:
            return

        tpl = self.config.comments
        pushed = await push_branch(project.path, w.branch_name)
        if not pushed:
            await self.jira.add_comment(
                w.issue_key,
                tpl.push_failed.format(branch=w.branch_name),
            )
            return

        try:
            git_provider = self._get_git_provider(project.git_provider)
            pr_title = f"{w.issue_key}: Symphony implementation"
            pr_desc = (
                f"**Jira:** [{w.issue_key}](https://{self.config.jira.site}/browse/{w.issue_key})\n\n"
                f"## Summary\n{w.output[:2000]}\n\n"
                f"---\n*Auto-generated by Jira Symphony*"
            )
            pr_url = await git_provider.create_pull_request(
                project.git_remote,
                w.branch_name,
                project.pr_target_branch,
                pr_title,
                pr_desc,
            )
        except Exception:
            log.exception("%s: PR/MR creation failed", w.issue_key)
            await self.jira.add_comment(
                w.issue_key,
                tpl.pr_failed.format(branch=w.branch_name),
            )
            return

        summary_snippet = w.output[:2000] if w.output else ""
        await self.jira.add_comment(
            w.issue_key,
            tpl.completion.format(pr_url=pr_url, summary=summary_snippet),
        )

        await self.jira.transition_issue(
            w.issue_key, self.config.jira.transitions.qa
        )

        await cleanup_worktree(project.path, w.worktree_path)
        log.info("%s: done — PR/MR at %s", w.issue_key, pr_url)

    async def _handle_failure(self, w: Worker) -> None:
        max_attempts = self.config.claude.max_retry_attempts
        backoff_base = self.config.claude.backoff_base_seconds
        project = self.config.get_project(w.project_key)

        if w.attempt < max_attempts:
            backoff = backoff_base * (2 ** (w.attempt - 1))
            log.info(
                "%s: retrying in %ds (attempt %d/%d)",
                w.issue_key, backoff, w.attempt + 1, max_attempts,
            )
            await asyncio.sleep(backoff)

            w.attempt += 1
            w.status = WorkerStatus.PENDING
            if project:
                issue = await self.jira.get_issue(w.issue_key)
                prompt = self.renderer.render(issue)
                cw = ClaudeWorker(w, project, self.config.claude, prompt)
                await cw.start()
                await self.state.upsert_worker(w)
                self._workers[w.issue_key] = cw
                asyncio.create_task(self._monitor_worker(cw))
        else:
            log.error("%s: max retries reached", w.issue_key)
            await self.jira.add_comment(
                w.issue_key,
                self.config.comments.all_attempts_failed.format(
                    max_attempts=max_attempts, error=w.error[:500],
                ),
            )
            await self.jira.transition_issue(
                w.issue_key, self.config.jira.transitions.to_do
            )
            if project:
                await cleanup_worktree(project.path, w.worktree_path)

    async def _recover_stale_workers(self) -> None:
        active = await self.state.get_active_workers()
        for w in active:
            log.warning("Recovering stale worker: %s", w.issue_key)
            w.status = WorkerStatus.FAILED
            w.error = "Orchestrator restarted — previous session lost"
            w.finished_at = datetime.now()
            await self.state.upsert_worker(w)
