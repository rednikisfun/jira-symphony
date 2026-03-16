"""Claude Code subprocess management with real-time progress streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .config import ClaudeConfig
from .models import ProjectConfig, Worker, WorkerStatus

log = logging.getLogger(__name__)

MAX_LOG_LINES = 200


def _deterministic_session_id(issue_key: str, attempt: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"symphony:{issue_key}:{attempt}"))


@dataclass
class WorkerProgress:
    """Real-time progress tracked from stream-json output."""
    current_activity: str = "starting"
    current_tool: str | None = None
    files_touched: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    message_count: int = 0
    tool_use_count: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    log_lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))

    def to_dict(self) -> dict:
        return {
            "current_activity": self.current_activity,
            "current_tool": self.current_tool,
            "files_touched": self.files_touched[-20:],
            "tools_used": self.tools_used[-20:],
            "message_count": self.message_count,
            "tool_use_count": self.tool_use_count,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "log_tail": list(self.log_lines)[-50:],
        }


class ClaudeWorker:
    """Manages a single Claude Code process with real-time progress tracking."""

    def __init__(
        self,
        worker: Worker,
        project: ProjectConfig,
        claude_cfg: ClaudeConfig,
        prompt: str,
    ) -> None:
        self.worker = worker
        self._project = project
        self._cfg = claude_cfg
        self._prompt = prompt
        self._proc: asyncio.subprocess.Process | None = None
        self.progress = WorkerProgress()
        self._stderr_buf: list[str] = []
        self._result_text: str = ""

    async def start(self) -> None:
        """Spawn the Claude Code process and begin streaming output."""
        w = self.worker
        w.session_id = _deterministic_session_id(w.issue_key, w.attempt)

        cmd_args = [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", self._cfg.model,
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", str(self._cfg.max_budget_usd),
            "--session-id", w.session_id,
            "--effort", "max",
        ]

        for extra_dir in self._project.extra_dirs:
            cmd_args.extend(["--add-dir", extra_dir])

        log.info(
            "Spawning Claude for %s in %s (attempt %d)",
            w.issue_key, w.worktree_path, w.attempt,
        )

        self._proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            cwd=w.worktree_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._proc.stdin.write(self._prompt.encode())
        await self._proc.stdin.drain()
        self._proc.stdin.close()

        w.pid = self._proc.pid
        w.status = WorkerStatus.RUNNING
        w.started_at = datetime.now()
        self.progress.current_activity = "running"
        log.info("%s: started (PID %d)", w.issue_key, w.pid)

    async def stream_and_wait(self, timeout_seconds: float) -> None:
        """Stream stdout line by line, tracking progress. Respects timeout."""
        assert self._proc is not None
        w = self.worker

        async def _read_stderr() -> None:
            assert self._proc.stderr is not None
            async for raw_line in self._proc.stderr:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    self._stderr_buf.append(line)
                    if len(self._stderr_buf) > 100:
                        self._stderr_buf = self._stderr_buf[-100:]

        async def _read_stdout() -> None:
            assert self._proc.stdout is not None
            async for raw_line in self._proc.stdout:
                line = raw_line.decode(errors="replace").rstrip()
                if not line:
                    continue
                self.progress.log_lines.append(line)
                self._parse_stream_event(line)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _read_stdout(),
                    _read_stderr(),
                    self._proc.wait(),
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning("%s: timed out after %ds", w.issue_key, timeout_seconds)
            self._proc.kill()
            await self._proc.wait()
            w.status = WorkerStatus.TIMED_OUT
            w.error = f"Timed out after {timeout_seconds}s"
            w.finished_at = datetime.now()
            self.progress.current_activity = "timed out"
            return

        w.finished_at = datetime.now()
        rc = self._proc.returncode

        if rc == 0:
            w.status = WorkerStatus.COMPLETED
            w.output = self._result_text or self._last_assistant_text()
            self.progress.current_activity = "completed"
            log.info("%s: completed successfully", w.issue_key)
        else:
            w.status = WorkerStatus.FAILED
            w.error = "\n".join(self._stderr_buf[-20:])
            self.progress.current_activity = "failed"
            log.error("%s: failed (rc=%d): %s", w.issue_key, rc, w.error[:200])

    def _parse_stream_event(self, line: str) -> None:
        """Parse a single stream-json line and update progress."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        etype = event.get("type")

        if etype == "assistant":
            self.progress.message_count += 1
            self.progress.current_activity = "thinking"
            self.progress.current_tool = None
            # Extract text for fallback result
            content = event.get("message", {}).get("content", [])
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        self._result_text = block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        self.progress.tool_use_count += 1
                        self.progress.current_tool = tool_name
                        self.progress.current_activity = f"using {tool_name}"
                        if tool_name not in self.progress.tools_used:
                            self.progress.tools_used.append(tool_name)
                        # Track files
                        inp = block.get("input", {})
                        for key in ("file_path", "path", "pattern", "command"):
                            val = inp.get(key)
                            if val and isinstance(val, str) and "/" in val:
                                if val not in self.progress.files_touched:
                                    self.progress.files_touched.append(val)

        elif etype == "tool_result":
            self.progress.current_activity = "thinking"
            self.progress.current_tool = None

        elif etype == "result":
            self._result_text = event.get("result", "")
            self.progress.cost_usd = event.get("cost_usd", 0.0)
            self.progress.duration_ms = event.get("duration_ms", 0)
            self.progress.current_activity = "finished"

    def _last_assistant_text(self) -> str:
        """Fallback: extract last text from log lines."""
        for line in reversed(list(self.progress.log_lines)):
            try:
                msg = json.loads(line)
                if msg.get("type") == "assistant":
                    for block in msg.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return ""

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            self.progress.current_activity = "killed"
