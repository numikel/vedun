"""Daily budget for paid API calls (embeddings + OpenRouter rerank), kept in ``state.db`` per UTC day.

The library already adds the cost of every call to ``Embedder._shared_cost`` (usage reported by OpenRouter). This module
moves that counter into a ledger (``flush``) after each request / job and resets it, so the library's per-process cost
guard keeps working as a per-window guard while the ledger is the real daily total. Attribution is approximate under
concurrency (a call that lands between the read and the reset is lost, microseconds), which is fine for a safety cap.

Jev (TypeSafe) reports no price, so its calls count as ``VEDUN_JEV_COST_PER_CALL_USD`` (default 0).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from vedun import config as C
from vedun.embed import Embedder
from vedun.rerank import RERANK_STATS
from vedun.state import connect_state

from . import settings as S

_lock = threading.Lock()
_process_total = 0.0
_jev_seen = 0


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def flush() -> float:
    """Move newly accrued cost into today's ledger row; returns today's total spend (USD)."""
    global _process_total, _jev_seen
    with _lock:
        delta, Embedder._shared_cost = Embedder._shared_cost, 0.0
        jev_calls = RERANK_STATS["r3"].calls
        per_call = float(C.env("JEV_COST_PER_CALL_USD", "0") or 0)
        delta += max(0, jev_calls - _jev_seen) * per_call
        _jev_seen = jev_calls
        _process_total += delta
        conn = connect_state()
        try:
            if delta > 0:
                conn.execute(
                    "INSERT INTO budget_ledger(day, spent_usd, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET spent_usd = spent_usd + excluded.spent_usd, updated_at = excluded.updated_at",
                    (_today(), delta, datetime.now(UTC).isoformat(timespec="seconds")),
                )
            r = conn.execute("SELECT spent_usd FROM budget_ledger WHERE day=?", (_today(),)).fetchone()
        finally:
            conn.close()
        return float(r[0]) if r else 0.0


def process_total() -> float:
    """Cost accrued by this process since start (after a ``flush``)."""
    flush()
    return _process_total


def spent_today() -> float:
    return flush()


def remaining() -> float:
    return max(0.0, S.daily_budget_usd() - spent_today())


def exhausted() -> bool:
    return spent_today() >= S.daily_budget_usd()


EXHAUSTED_MESSAGE = "daily API budget used up"
