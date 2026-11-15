"""Throwaway app for local UI work: ``python -m vedun.web.dev_app`` (127.0.0.1:8766).

Serves only the web router and ``/static``. With ``VEDUN_WEB_DEMO=1`` it also fakes the optional
integrations (jobs module, ``/api/collection``, ``/api/stats``, ``/api/citations/latest`` and a sample
sync report) so every state of the UI can be looked at without the real API. Never used in production.
"""

from __future__ import annotations

import sys
import tempfile
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles

from vedun import config as C

from . import STATIC_DIR, router


# ---------------------------------------------------------------------------- demo data
@dataclass
class _Job:
    id: str
    kind: str
    status: str
    progress: float | None = None
    message: str = ""
    log_tail: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    error: str | None = None


def _iso(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def install_demo(report_dir: Path) -> None:
    """Register fake ``vedun.server.jobs`` and write a sample sync report into ``report_dir``."""
    from vedun.report import ChangedUnit, DiffReport, Renumber, render_diff

    rep = DiffReport("3-14", "2026-04", "2026-10")
    rep.removed = ["4.1.1", "4.1.2", "Table 7", "6.9.9"]
    rep.added = ["4.2.1.1", "Table 4.3.1", "4.2.2"]
    rep.changed = [
        ChangedUnit("4.1.4", "Keep stored pallets at least [-1.0 m-] {+1.5 m+} from heaters."),
        ChangedUnit("4.1.3", "Inspect the storage area {+at least+} once a week and record the result."),
    ]
    rep.renumbered = [
        Renumber("4.1.1", "4.2.1.1", 0.97, "Pallet storage", "Pallet storage"),
        Renumber("Table 7", "Table 4.3.1", 0.91, "Inspection schedule", "Inspection schedule"),
    ]
    rep.possible = [Renumber("6.9.9", "4.2.2", 0.64)]
    rep.unchanged = 41
    rep.changes_section = (
        "April 2027. Completely revised.\n\nA. Added the pallet storage clauses.\nB. Renumbered clauses."
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "sync_2026-11-10.md").write_text(
        "# vedun sync report 2026-11-10 23:14\n\n"
        "Added: 5-21; replaced: 3-14; removed (PDF gone): -; unchanged: 3.\n\n"
        "WARNING: demo data, not a real sync\n\n"
        + render_diff(rep)
        + "\n## Document 5-21: new document, edition 2026-07 (212 units, 380 chunks)\n\n",
        encoding="utf-8",
    )

    t0 = time.time()
    state: dict[str, _Job] = {
        "j-done": _Job(
            "j-done0001",
            "sync",
            "done",
            100,
            "Finished: 1 replaced, 1 new",
            ["converted 3-14", "embedded 212 chunks", "report written"],
            created_at=_iso(190),
            started_at=_iso(189),
            finished_at=_iso(171),
            result={"report": "reports/sync_2026-11-10.md"},
        ),
        "j-fail": _Job(
            "j-fail0002",
            "catalog_refresh",
            "failed",
            None,
            "Could not fetch the catalog",
            ["GET https://catalog.example.org/... timeout"],
            created_at=_iso(60),
            started_at=_iso(60),
            finished_at=_iso(59),
            error="ConnectTimeout: catalog unavailable",
        ),
        "j-cit": _Job(
            "j-cit0003",
            "check_citations",
            "done",
            100,
            "Checked 48 files",
            [],
            created_at=_iso(30),
            started_at=_iso(30),
            finished_at=_iso(29),
            result=_citations_payload()["results"],
        ),
    }

    def running() -> _Job:
        elapsed = time.time() - t0
        prog = min(1.0, (elapsed % 90) / 90)
        return _Job(
            "j-run0004",
            "sync",
            "running",
            prog,
            f"Embedding chunks of ASC 5-21 ({int(prog * 380)}/380)",
            [f"embedding batch {i}" for i in range(1, int(prog * 12) + 3)],
            created_at=_iso(2),
            started_at=_iso(2),
        )

    jobs = types.ModuleType("vedun.server.jobs")
    jobs.list_jobs = lambda limit=50: [running(), *state.values()][:limit]  # type: ignore[attr-defined]
    jobs.get_job = lambda jid: running() if jid == "j-run0004" else next(j for j in state.values() if j.id == jid)  # type: ignore[attr-defined]

    def submit_job(kind: str, params: dict[str, Any]) -> str:
        return "j-run0004"

    jobs.submit_job = submit_job  # type: ignore[attr-defined]
    # only the jobs module is faked: the real package stays importable for /connect (mcp_app, mcp_tools, settings)
    import vedun.server as pkg

    pkg.jobs = jobs  # type: ignore[attr-defined]
    sys.modules["vedun.server.jobs"] = jobs


def _citations_payload() -> dict[str, Any]:
    rows = [
        {
            "file": "notes/storage_review.md",
            "line": 41,
            "citation": "ASC 3-14 Section 4.2.1.6",
            "doc_id": "3-14",
            "unit_id": "2.3.4.1.6",
            "status": "OK",
            "suggestion": "",
        },
        {
            "file": "notes/site_audit.md",
            "line": 118,
            "citation": "ASC 3-14 Section 4.1.1",
            "doc_id": "3-14",
            "unit_id": "2.3.2.1",
            "status": "UNKNOWN UNIT",
            "suggestion": "renumbered to 4.2.1.1; nearest: 4.1.2, 4.1.3",
        },
        {
            "file": "notes/site_audit.md",
            "line": 131,
            "citation": "ASC 2-40 Table 7",
            "doc_id": "2-40",
            "unit_id": "Table 9a",
            "status": "UNKNOWN UNIT",
            "suggestion": "nearest: Table 9, Table 10",
        },
        {
            "file": "mail/contractor.md",
            "line": 7,
            "citation": "ASC 4-2 Section 2.2.8",
            "doc_id": "4-2",
            "unit_id": "2.2.8.1",
            "status": "DOCUMENT NOT INDEXED",
            "suggestion": "",
        },
        {
            "file": "mail/contractor.md",
            "line": 19,
            "citation": "ASC 7-11 Section 2.4.1",
            "doc_id": "7-11",
            "unit_id": "2.4.1",
            "status": "OK",
            "suggestion": "note: cited edition 2024-04, current 2025-04",
        },
    ]
    return {"generated_at": _iso(29), "results": rows}


def demo_api() -> APIRouter:
    api = APIRouter(prefix="/api")

    @api.get("/collection")
    def collection() -> list[dict[str, Any]]:
        return [
            {
                "doc_id": "3-14",
                "title": "Storage of Combustible Materials",
                "catalog_status": "outdated",
                "latest_edition": "April 2027",
            },
            {"doc_id": "2-40", "catalog_status": "current"},
            {"doc_id": "5-21", "catalog_status": "current"},
            {"doc_id": "8-5", "catalog_status": "current"},
            {"doc_id": "7-11", "catalog_status": "withdrawn"},
            {
                "doc_id": "4-2",
                "title": "Clean Agent Extinguishing Systems",
                "catalog_status": "missing_locally",
            },
        ]

    @api.get("/stats")
    def stats() -> dict[str, Any]:
        return {"cost_usd": 0.37, "last_sync": _iso(171)}

    @api.get("/citations/latest")
    def citations() -> dict[str, Any]:
        return _citations_payload()

    return api


# ---------------------------------------------------------------------------- app
def create_app(demo: bool = False) -> FastAPI:
    app = FastAPI(title="vedun (dev)")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    if demo:
        report_dir = Path(C.env("WEB_DEMO_DIR") or tempfile.gettempdir()) / "vedun_web_demo_reports"
        C.REPORT_DIR = report_dir
        install_demo(report_dir)
        app.include_router(demo_api())
    app.include_router(router)
    return app


app = create_app(demo=C.env("WEB_DEMO") == "1")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="info")
