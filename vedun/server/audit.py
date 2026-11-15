"""Append-only audit log ``<reports>/audit.jsonl`` for state-changing actions.

One JSON object per line: ``ts``, ``action``, ``caller`` (dashboard | api | mcp | internal | unknown), ``job_id``,
``outcome`` and an optional short ``detail``. Never written: credentials, query text, document content, file paths of
the user's notes. The file lives in the reports folder, i.e. on the host bind mount, so it survives the container.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from vedun import config as C

log = logging.getLogger("vedun.audit")
_lock = threading.Lock()

#: who is behind the current request / job (set by the security middleware, copied into worker threads)
CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("vedun_caller", default="internal")

_ALLOWED = {"ts", "action", "caller", "job_id", "outcome", "detail"}


def log_event(
    action: str, *, job_id: str | None = None, outcome: str = "ok", detail: str | None = None, caller: str | None = None
) -> None:
    """Append one entry; failures to write are logged, never raised (an audit problem must not break a request)."""
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "action": action,
        "caller": caller or CALLER.get(),
        "job_id": job_id,
        "outcome": outcome,
    }
    if detail:
        entry["detail"] = detail[:200]
    line = json.dumps({k: v for k, v in entry.items() if k in _ALLOWED}, ensure_ascii=False) + "\n"
    try:
        with _lock:
            C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
            with (C.REPORT_DIR / "audit.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as e:
        log.error("audit log not written: %s", e)


def read_tail(n: int = 100) -> list[dict[str, Any]]:
    """Last ``n`` entries (for tests and diagnostics)."""
    p = C.REPORT_DIR / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[-n:] if x.strip()]
