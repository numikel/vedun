"""Sidecar state database (``state.db``): job history and the source catalog cache.

Kept apart from ``vedun.db`` on purpose: the knowledge base stays a read-mostly file with a single writer
(``sync``), while the service writes job progress every few seconds. The sidecar uses WAL. In the container it
is the host folder ``.state-docker/`` (bind mount; WAL verified with concurrent writers); host runs use ``.state/``,
so one SQLite file is never shared across the Windows/WSL boundary. ``VEDUN_STATE_JOURNAL=DELETE`` turns WAL off
if a filesystem cannot do it. Also holds the daily budget ledger and the last citation results.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config as C


def state_path() -> Path:
    """Location of ``state.db`` (``VEDUN_STATE_DIR``, default ``<root>/.state``)."""
    return C.STATE_DIR / "state.db"


def connect_state(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the sidecar DB (creating it) in WAL mode; one connection per thread."""
    p = Path(path) if path else state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA journal_mode={_journal_mode()}")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, params TEXT NOT NULL, status TEXT NOT NULL,
            progress REAL, message TEXT NOT NULL DEFAULT '', log_tail TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, result TEXT, error TEXT,
            cost_usd REAL, api_calls INTEGER);
        CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);
        CREATE TABLE IF NOT EXISTS catalog_snapshot(
            id INTEGER PRIMARY KEY CHECK (id = 1), fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS citations_latest(
            id INTEGER PRIMARY KEY CHECK (id = 1), generated_at TEXT NOT NULL, job_id TEXT,
            repo TEXT, n_files INTEGER, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS budget_ledger(
            day TEXT PRIMARY KEY, spent_usd REAL NOT NULL DEFAULT 0, updated_at TEXT);
        """
    )
    if "caller" not in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}:
        conn.execute("ALTER TABLE jobs ADD COLUMN caller TEXT")  # who submitted the job (audit)
    return conn


def _journal_mode() -> str:
    """``WAL`` by default; ``VEDUN_STATE_JOURNAL=DELETE`` for a filesystem without shared-memory support."""
    m = C.env("STATE_JOURNAL", "WAL").upper()
    return m if m in ("WAL", "DELETE", "TRUNCATE") else "WAL"
