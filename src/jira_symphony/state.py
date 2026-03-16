"""SQLite-backed state persistence for worker tracking."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

from .config import DATA_DIR
from .models import Worker, WorkerStatus

log = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "symphony.db"

SCHEMA = """\
CREATE TABLE IF NOT EXISTS workers (
    issue_key TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    pid INTEGER,
    session_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    output TEXT DEFAULT '',
    error TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS manual_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key TEXT NOT NULL,
    project_key TEXT,
    created_at TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);
"""


class StateStore:
    """Async SQLite state persistence."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = str(db_path or DB_PATH)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def upsert_worker(self, w: Worker) -> None:
        assert self._db
        await self._db.execute(
            """\
            INSERT INTO workers
                (issue_key, project_key, worktree_path, branch_name, status,
                 pid, session_id, attempt, started_at, finished_at, output, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_key) DO UPDATE SET
                project_key=excluded.project_key,
                worktree_path=excluded.worktree_path,
                branch_name=excluded.branch_name,
                status=excluded.status,
                pid=excluded.pid,
                session_id=excluded.session_id,
                attempt=excluded.attempt,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                output=excluded.output,
                error=excluded.error
            """,
            (
                w.issue_key,
                w.project_key,
                w.worktree_path,
                w.branch_name,
                w.status.value,
                w.pid,
                w.session_id,
                w.attempt,
                w.started_at.isoformat() if w.started_at else None,
                w.finished_at.isoformat() if w.finished_at else None,
                w.output,
                w.error,
            ),
        )
        await self._db.commit()

    async def get_active_workers(self) -> list[Worker]:
        """Get all workers in RUNNING or PENDING status."""
        assert self._db
        cursor = await self._db.execute(
            "SELECT * FROM workers WHERE status IN ('running', 'pending')"
        )
        rows = await cursor.fetchall()
        return [self._row_to_worker(r) for r in rows]

    async def get_worker(self, issue_key: str) -> Worker | None:
        assert self._db
        cursor = await self._db.execute(
            "SELECT * FROM workers WHERE issue_key = ?", (issue_key,)
        )
        row = await cursor.fetchone()
        return self._row_to_worker(row) if row else None

    async def get_all_workers(self) -> list[Worker]:
        assert self._db
        cursor = await self._db.execute(
            "SELECT * FROM workers ORDER BY started_at DESC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_worker(r) for r in rows]

    # ── Manual queue ──────────────────────────────────────

    async def enqueue_manual(self, issue_key: str, project_key: str | None) -> None:
        """Add an issue to the manual dispatch queue."""
        assert self._db
        await self._db.execute(
            "INSERT INTO manual_queue (issue_key, project_key, created_at) VALUES (?, ?, ?)",
            (issue_key, project_key, datetime.now().isoformat()),
        )
        await self._db.commit()

    async def dequeue_manual(self) -> list[tuple[str, str | None]]:
        """Return and mark as processed all unprocessed manual items."""
        assert self._db
        cursor = await self._db.execute(
            "SELECT issue_key, project_key FROM manual_queue WHERE processed = 0 ORDER BY id"
        )
        rows = await cursor.fetchall()
        if rows:
            await self._db.execute(
                "UPDATE manual_queue SET processed = 1 WHERE processed = 0"
            )
            await self._db.commit()
        return [(r[0], r[1]) for r in rows]

    @staticmethod
    def _row_to_worker(row: tuple) -> Worker:
        return Worker(
            issue_key=row[0],
            project_key=row[1],
            worktree_path=row[2],
            branch_name=row[3],
            status=WorkerStatus(row[4]),
            pid=row[5],
            session_id=row[6],
            attempt=row[7],
            started_at=datetime.fromisoformat(row[8]) if row[8] else None,
            finished_at=datetime.fromisoformat(row[9]) if row[9] else None,
            output=row[10] or "",
            error=row[11] or "",
        )
