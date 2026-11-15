"""Background jobs: one at a time, persisted in the sidecar state DB.

Design (see the README):

* **One worker thread + a FIFO queue.** ``vedun.db`` has a single writer (``sync``); running jobs strictly
  one after another removes every write-write conflict and keeps CPU-heavy Docling conversions from
  overlapping. HTTP requests never wait for a job, they only enqueue it and poll :func:`get_job`.
* **Persistence in ``state.db``** (:mod:`vedun.state`, WAL), not in ``vedun.db``: the KB file stays untouched by
  job bookkeeping (progress is written every few hundred ms), and the history survives a container
  re-creation because ``state.db`` lives on a named volume. A job that was queued or running when the process
  died is marked ``failed`` ("interrupted by restart") on the next :func:`start`.
* The public contract (used by the dashboard through ``importlib``): :func:`submit_job`, :func:`get_job`,
  :func:`list_jobs`, :class:`JobInfo`.

Job kinds and params:

``sync``            ``only`` (sheet numbers), ``fetch_from_source`` (bool), ``include_missing`` (bool), ``uploaded``
                    (PDF file names staged in ``<pdf_dir>/.incoming``), ``dry_run``, ``force``, ``no_embed``.
``check_citations`` ``repo`` (path inside the configured repo folder; default = the whole repo folder).
``catalog_refresh`` none.

Cancellation is cooperative (:func:`cancel_job`): a queued job is marked ``cancelled`` at once; a running one gets a
request that its runner honours at the next safe point (``sync``: between sheets, never inside a transaction;
``check_citations``: between files). ``catalog_refresh`` is a single request with no safe point and is refused while
running. A runner reports an early stop by returning ``{"cancelled": True, ...}``.
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import sqlite3
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from vedun import catalog as K
from vedun import config as C
from vedun import db as D
from vedun.check_citations import check_files, iter_text_files
from vedun.embed import STATS
from vedun.state import connect_state

from . import audit, budget
from . import settings as S

JobKind = Literal["sync", "check_citations", "catalog_refresh"]
KINDS = ("sync", "check_citations", "catalog_refresh")
LOG_TAIL_LINES = 60
_SAVE_EVERY_S = 0.4


@dataclass
class JobInfo:
    """A job as seen by the API and the dashboard (``progress`` is 0..1 or ``None``)."""

    id: str
    kind: str
    params: dict[str, Any]
    status: str  # queued | running | done | failed | cancelled
    progress: float | None
    message: str
    log_tail: list[str]
    created_at: str
    started_at: str | None
    finished_at: str | None
    result: dict[str, Any] | None
    error: str | None
    cost_usd: float | None = None
    api_calls: int | None = None
    caller: str | None = None  # dashboard | api | mcp | internal (audit)
    cancel_requested: bool = False  # live flag of this process (not persisted): a running job was asked to stop

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_job(r: sqlite3.Row) -> JobInfo:
    return JobInfo(
        id=r["id"],
        kind=r["kind"],
        params=json.loads(r["params"]),
        status=r["status"],
        progress=r["progress"],
        message=r["message"],
        log_tail=json.loads(r["log_tail"]),
        created_at=r["created_at"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        result=json.loads(r["result"]) if r["result"] else None,
        error=r["error"],
        cost_usd=r["cost_usd"],
        api_calls=r["api_calls"],
        caller=r["caller"],
        cancel_requested=r["id"] in _cancel_requested and r["status"] == "running",
    )


def _save(job: JobInfo) -> None:
    conn = connect_state()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jobs(id, kind, params, status, progress, message, log_tail, created_at, "
            "started_at, finished_at, result, error, cost_usd, api_calls, caller) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.id,
                job.kind,
                json.dumps(job.params, ensure_ascii=False, default=str),
                job.status,
                job.progress,
                job.message,
                json.dumps(job.log_tail, ensure_ascii=False),
                job.created_at,
                job.started_at,
                job.finished_at,
                json.dumps(job.result, ensure_ascii=False, default=str) if job.result is not None else None,
                job.error,
                job.cost_usd,
                job.api_calls,
                job.caller,
            ),
        )
    finally:
        conn.close()


# ============================================================================ public contract
def get_job(job_id: str) -> JobInfo:
    """The job with this id; raises ``KeyError`` when unknown."""
    conn = connect_state()
    try:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if r is None:
        raise KeyError(job_id)
    return _row_to_job(r)


def list_jobs(limit: int = 50) -> list[JobInfo]:
    """Newest first."""
    conn = connect_state()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (max(1, min(int(limit), 500)),)
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_job(r) for r in rows]


def jobs_summary() -> dict[str, Any]:
    """Counts by status, total persisted API cost and the finish time of the last successful sync."""
    conn = connect_state()
    try:
        by = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        cost = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM jobs").fetchone()[0]
        last = conn.execute(
            "SELECT finished_at FROM jobs WHERE kind='sync' AND status='done' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return {"by_status": by, "cost_usd": round(float(cost), 6), "last_sync": last[0] if last else None}


def submit_job(kind: JobKind, params: dict[str, Any] | None = None) -> str:
    """Enqueue a job and return its id (starts the worker thread on first use)."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}; known: {', '.join(KINDS)}")
    params = dict(params or {})
    json.dumps(params)  # reject non-JSON params early (TypeError)
    job = JobInfo(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        params=params,
        status="queued",
        progress=None,
        message="W kolejce",
        log_tail=[],
        created_at=_now(),
        started_at=None,
        finished_at=None,
        result=None,
        error=None,
        caller=audit.CALLER.get(),
    )
    _save(job)
    audit.log_event(kind, job_id=job.id, outcome="queued", caller=job.caller)
    start()
    _queue.put(job.id)
    return job.id


# ============================================================================ worker
_queue: queue.Queue[str | None] = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
# Serialises "queued -> running" (worker) against "queued -> cancelled" (request thread); guards _cancel_requested.
_state_lock = threading.Lock()
_cancel_requested: set[str] = set()  # ids of running jobs asked to stop (in memory: a restart fails them anyway)
CANCELLABLE_WHILE_RUNNING = frozenset({"sync", "check_citations"})  # kinds whose runner has a safe point


class NotCancellable(Exception):
    """The job already finished (``reason`` "finished") or runs a kind with no safe point (``reason`` "no_safe_point")."""

    def __init__(self, reason: Literal["finished", "no_safe_point"], message: str) -> None:
        super().__init__(message)
        self.reason = reason


def cancel_job(job_id: str) -> JobInfo:
    """Cancel a job; ``KeyError`` when unknown, :class:`NotCancellable` when it cannot be stopped.

    Queued -> ``cancelled`` immediately (the worker skips it; staged uploads are discarded). Running -> a stop is
    requested and the returned job still says ``running`` with ``cancel_requested`` true; the runner ends the job as
    ``cancelled`` at its next safe point (idempotent: asking twice is fine). A job that finishes before reaching a
    safe point ends as ``done``: the request came too late.
    """
    with _state_lock:
        job = get_job(job_id)
        if job.status == "queued":
            job.status, job.finished_at, job.message = "cancelled", _now(), "Anulowane w kolejce"
            job.cost_usd, job.api_calls = 0.0, 0
            job.result = {"cancelled": True, "summary": job.message}
            _save(job)
            outcome = "cancelled"
        elif job.status == "running":
            if job.kind not in CANCELLABLE_WHILE_RUNNING:
                raise NotCancellable(
                    "no_safe_point", f"a running {job.kind} job cannot be interrupted; it ends by itself shortly"
                )
            _cancel_requested.add(job_id)
            job.cancel_requested = True
            outcome = "requested"
        else:
            raise NotCancellable("finished", f"job {job_id} is already {job.status}")
    if outcome == "cancelled":
        _discard_staged(job.params)
    audit.log_event("job_cancel", job_id=job_id, outcome=outcome, detail=job.kind, caller=audit.CALLER.get())
    return job


def _discard_staged(params: dict[str, Any]) -> None:
    """PDFs uploaded for a sync that will never run would otherwise sit in ``.incoming`` forever."""
    for name in params.get("uploaded") or []:
        if re.fullmatch(S.PDF_NAME_RE, str(name)):  # trust boundary: a bare file name, nothing else
            (S.incoming_dir() / str(name)).unlink(missing_ok=True)


def start() -> None:
    """Start the worker thread once (idempotent); marks jobs interrupted by a previous run as failed."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        conn = connect_state()
        try:
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, error='interrupted by a restart of the service', "
                "message='Przerwane przez restart usługi' WHERE status IN ('queued','running')",
                (_now(),),
            )
        finally:
            conn.close()
        _thread = threading.Thread(target=_worker, name="vedun-jobs", daemon=True)
        _thread.start()


def stop(timeout: float = 2.0) -> None:
    """Ask the worker to exit after the current job (the thread is a daemon; a running job is abandoned)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        _queue.put(None)
        _thread.join(timeout)
    _thread = None


def worker_alive() -> bool:
    return _thread is not None and _thread.is_alive()


def queue_depth() -> int:
    return _queue.qsize()


class JobContext:
    """What a runner gets: logging, progress and cost bookkeeping that is saved (throttled) to the state DB."""

    def __init__(self, job: JobInfo) -> None:
        self.job = job
        self._last_save = 0.0

    def save(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_save >= _SAVE_EVERY_S:
            self._last_save = now
            _save(self.job)

    def cancelled(self) -> bool:
        """True once a stop was requested; runners poll it at their safe points."""
        return self.job.id in _cancel_requested

    def log(self, line: str) -> None:
        line = str(line).rstrip()[:300]
        self.job.log_tail = [*self.job.log_tail, line][-LOG_TAIL_LINES:]
        self.job.message = line
        self.save()

    def progress(self, frac: float | None, message: str | None = None) -> None:
        self.job.progress = None if frac is None else max(0.0, min(1.0, float(frac)))
        if message:
            self.job.message = message
        self.save()


def _worker() -> None:
    while True:
        job_id = _queue.get()
        if job_id is None:
            return
        job = _claim(job_id)
        if job is not None:
            _run(job)


def _claim(job_id: str) -> JobInfo | None:
    """Mark a still-queued job as running; ``None`` for one that vanished or was cancelled while waiting."""
    with _state_lock:  # status is re-read inside the lock: cancel_job may have won the race
        try:
            job = get_job(job_id)
        except KeyError:
            return None
        if job.status != "queued":
            return None
        job.status, job.started_at, job.progress, job.message = "running", _now(), 0.0, "Start"
        _save(job)
        return job


def _run(job: JobInfo) -> None:
    ctx = JobContext(job)
    cost0 = budget.process_total()
    calls0 = sum(s.calls for s in STATS.values())
    try:
        result = RUNNERS[job.kind](ctx, job.params)
        job.result = _jsonable(result)
        stopped = isinstance(result, dict) and bool(result.get("cancelled"))
        job.status = "cancelled" if stopped else "done"
        job.progress = job.progress if stopped else 1.0
        job.message = str(result.get("summary") or "Zakończono") if isinstance(result, dict) else "Zakończono"
    except Exception as e:  # noqa: BLE001 - a job must never kill the worker
        job.status = "failed"
        job.error = f"{type(e).__name__}: {str(e)[:500]}"
        job.message = f"Błąd: {str(e)[:200]}"
        job.log_tail = [*job.log_tail, *traceback.format_exc().strip().splitlines()[-4:]][-LOG_TAIL_LINES:]
    job.finished_at = _now()
    job.cost_usd = round(max(0.0, budget.process_total() - cost0), 6)
    job.api_calls = sum(s.calls for s in STATS.values()) - calls0
    job.cancel_requested = False
    ctx.save(force=True)
    with _state_lock:  # after the final save: a cancel arriving now sees a finished job and is refused
        _cancel_requested.discard(job.id)
    audit.log_event(
        job.kind,
        job_id=job.id,
        outcome=job.status,
        detail=f"cost {job.cost_usd} USD" + (f"; {job.error[:80]}" if job.error else ""),
        caller=job.caller,
    )


def _jsonable(o: Any) -> Any:
    return json.loads(json.dumps(o, ensure_ascii=False, default=str))


# ============================================================================ runners
def _run_catalog_refresh(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    src = K.load_source()
    ctx.log(f"GET {src.name} document catalog")
    cat = K.fetch_catalog(force=True, source=src)
    ctx.progress(0.6, f"Pobrano katalog: {len(cat.entries)} dokumentów")
    filled = 0
    conn = D.connect()
    try:
        filled = K.fill_source_item_ids(conn, cat)
        rows = K.catalog_status(conn, cat)
    finally:
        conn.close()
    counts = K.counts_by_status(rows)
    ctx.log(f"statuses: {counts}; source_item_id filled for {filled} indexed document(s)")
    return {
        "summary": f"Katalog: {len(cat.entries)} dokumentów ({counts})",
        "fetched_at": cat.fetched_at,
        "n_entries": len(cat.entries),
        "counts": counts,
        "source_item_ids_filled": filled,
        "warnings": cat.warnings,
        "outdated": [r.doc_id for r in rows if r.status == "outdated"],
        "withdrawn": [r.doc_id for r in rows if r.status == "withdrawn"],
    }


def resolve_repo(repo: str | None) -> Path:
    """The folder to scan: ``repo`` (absolute or relative to the repo folder), refused outside the repo folder."""
    base = S.repo_dir().resolve()
    if not repo:
        return base
    p = Path(repo)
    p = (p if p.is_absolute() else base / p).resolve()
    if p != base and not p.is_relative_to(base):
        raise ValueError(f"repo must be inside {base} (got {p})")
    if not p.is_dir():
        raise ValueError(f"not a directory: {p}")
    return p


def _run_check_citations(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    root = resolve_repo(params.get("repo"))
    ctx.log(f"scanning {root}")
    files = iter_text_files(root)
    ctx.log(f"{len(files)} text files")
    rows: list[dict[str, Any]] = []
    conn = S.open_readonly()
    try:
        for i, f in enumerate(files, start=1):
            if ctx.cancelled():  # nothing is stored: a partial scan must not become "the latest citations"
                msg = f"Anulowano po {i - 1}/{len(files)} plikach; wyniki nie zostały zapisane"
                return {"summary": msg, "cancelled": True, "repo": str(root), "n_files": len(files)}
            rows += [r.to_dict() for r in check_files(conn, [f], root)]
            if i % 25 == 0 or i == len(files):
                ctx.progress(i / max(1, len(files)), f"Sprawdzono {i}/{len(files)} plików")
    finally:
        conn.close()
    counts = {s: sum(r["status"] == s for r in rows) for s in ("OK", "UNKNOWN UNIT", "DOCUMENT NOT INDEXED")}
    generated = _now()
    sc = connect_state()
    try:
        sc.execute(
            "INSERT OR REPLACE INTO citations_latest(id, generated_at, job_id, repo, n_files, payload) "
            "VALUES(1, ?, ?, ?, ?, ?)",
            (generated, ctx.job.id, str(root), len(files), json.dumps(rows, ensure_ascii=False)),
        )
    finally:
        sc.close()
    return {
        "summary": f"{len(rows)} cytowań w {len(files)} plikach: {counts}",
        "repo": str(root),
        "n_files": len(files),
        "n_citations": len(rows),
        "counts": counts,
        "generated_at": generated,
    }


def latest_citations() -> dict[str, Any] | None:
    """Results of the last successful check-citations job, or ``None``."""
    conn = connect_state()
    try:
        r = conn.execute("SELECT * FROM citations_latest WHERE id=1").fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    rows = json.loads(r["payload"])
    return {
        "generated_at": r["generated_at"],
        "job_id": r["job_id"],
        "repo": r["repo"],
        "n_files": r["n_files"],
        "counts": {s: sum(x["status"] == s for x in rows) for s in ("OK", "UNKNOWN UNIT", "DOCUMENT NOT INDEXED")},
        "results": rows,
    }


_SYNC_DS = re.compile(r"^\[([^\]]+)\]")


def _run_sync(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    pdf_dir = C.DEFAULT_PDF_DIR
    pdf_dir.mkdir(parents=True, exist_ok=True)
    dry = bool(params.get("dry_run"))
    notes: list[str] = []
    if not dry and not params.get("no_embed") and budget.exhausted():
        raise RuntimeError(
            f"{budget.EXHAUSTED_MESSAGE} ({S.daily_budget_usd():.2f} USD): synchronizacja wymaga embeddingów"
        )

    # 1. PDFs uploaded through the API (staged in <pdf_dir>/.incoming): move next to the others
    new_files: dict[str, str] = {}  # ds -> new PDF file name (uploaded or downloaded)
    for name in params.get("uploaded") or []:
        if not re.fullmatch(S.PDF_NAME_RE, str(name)):  # trust boundary: a bare file name, nothing else
            raise ValueError(f"bad uploaded file name: {name!r}")
        src = S.incoming_dir() / name
        if not src.is_file():
            raise FileNotFoundError(f"uploaded file vanished: {name}")
        if not dry:
            src.replace(pdf_dir / name)
        new_files[name.split("_")[0]] = name
        ctx.log(f"uploaded {name}")

    # 2. newer editions from the configured source (never deletes anything)
    conn = D.connect()
    try:
        indexed = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")]
        # default scope: the indexed documents (a sync never silently grows a collection), except for an empty
        # knowledge base, whose first sync takes every document in the folder (a new install)
        default_scope = indexed or sorted(K.scan_pdfs(pdf_dir))
        only = list(params["only"]) if params.get("only") else None
        if params.get("fetch_from_source") and ctx.cancelled():  # download_updates has no safe point of its own
            notes.append("cancelled before the download from the source: nothing was fetched")
        elif params.get("fetch_from_source"):
            src = K.load_source()
            ctx.progress(0.02, f"Pobieranie katalogu {src.name}")
            cat = K.fetch_catalog(force=True, source=src)
            scope = only if only is not None else default_scope
            if dry:
                st = [s for s in K.catalog_status(None, cat, pdf_dir) if s.doc_id in scope and s.status == "outdated"]
                notes.append("dry run: would download " + (", ".join(s.doc_id for s in st) or "nothing"))
            else:
                rep = K.download_updates(
                    pdf_dir,
                    catalog=cat,
                    only=scope,
                    include_missing=bool(params.get("include_missing")),
                    source=src,
                    log=ctx.log,
                )
                audit.log_event(
                    "source_download",
                    job_id=ctx.job.id,
                    outcome=f"{len(rep.downloaded)} ok, {len(rep.errors)} failed",
                    detail=", ".join(d.file for d in rep.downloaded),
                    caller=ctx.job.caller,
                )
                for d in rep.downloaded:
                    new_files[d.doc_id] = d.file
                for ds, err in rep.errors.items():
                    notes.append(f"download of {ds} failed: {err}")
        # 3. sync only what is in scope: asked-for documents, or the default scope + the new files
        scope_ds = sorted({*(only if only is not None else default_scope), *new_files})
        plan = D.plan_sync(conn, pdf_dir, scope_ds, bool(params.get("force")))
        n = max(1, len(plan.add) + len(plan.replace) + len(plan.remove))
        ctx.log(f"plan: add {plan.add} replace {plan.replace} remove {plan.remove} unchanged {len(plan.unchanged)}")
        seen: list[str] = []

        def on_log(line: str) -> None:
            m = _SYNC_DS.match(line)
            if m and m.group(1) not in seen:
                seen.append(m.group(1))
            ctx.progress(max(0, len(seen) - 0.5) / n if seen else 0.05, line[:300])
            ctx.log(line)

        res = None
        failure: Exception | None = None
        try:
            res = D.sync(
                conn,
                pdf_dir,
                only=scope_ds,
                text_models=C.TEXT_MODEL_KEYS,
                image_models=C.IMAGE_MODEL_KEYS,
                embed=not params.get("no_embed"),
                dry_run=dry,
                force=bool(params.get("force")),
                cost_guard=budget.remaining(),  # the daily budget, not the library's 1 USD pilot guard
                should_stop=ctx.cancelled,
                log=on_log,
            )
        except Exception as e:  # noqa: BLE001 - quarantine the new PDFs below, then re-raise
            failure = e

        # 4. old PDFs go only after the new edition is in the DB; a failed one is quarantined
        retired: dict[str, list[str]] = {}
        rejected: list[str] = []
        pending: dict[str, str] = {}  # new PDFs whose sheet a cancel skipped: valid, just not indexed yet
        skipped = set(res.skipped) if res is not None else set()
        if not dry:
            for ds, fname in new_files.items():
                if ds in skipped:  # neither retire the old edition nor quarantine: the next sync indexes it
                    pending[ds] = fname
                    continue
                row = conn.execute("SELECT edition FROM documents WHERE doc_id=?", (ds,)).fetchone()
                want = fname.split("_")[1].removesuffix(".pdf")
                if row is not None and row["edition"].replace("-", "") == want:
                    retired[ds] = K.retire_old_pdfs(pdf_dir, ds, fname)
                else:
                    q = pdf_dir / ".incoming" / "rejected"
                    q.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(pdf_dir / fname), str(q / fname))
                    rejected.append(fname)
        if failure is not None:
            raise failure
        # nothing succeeded: the job failed (a cancel keeps its own status instead)
        if res is not None and res.failed and not (res.results or res.healed or res.cancelled):
            raise RuntimeError("; ".join(f"{ds}: {err}" for ds, err in res.failed.items()))

        # 5. source_item_id into the documents table (from the cached catalog; no extra request)
        filled = 0
        cat_cached = K.cached_catalog()
        if cat_cached is not None and not dry:
            filled = K.fill_source_item_ids(conn, cat_cached)
    finally:
        conn.close()

    assert res is not None
    for r in res.results:
        bad = [f"{m.model_key}/{m.kind}:{m.status}" for m in r.models if m.status not in ("complete", "pending")]
        if bad:
            notes.append(f"{r.doc_id}: embeddings incomplete ({', '.join(bad)}); the next sync heals them")
    for ds, err in res.failed.items():
        notes.append(f"{ds}: failed, previous state kept ({err}); run sync again for this sheet")
    report = Path(res.report_path).name if res.report_path else None
    n_add = sum(r.action == "added" for r in res.results) if not dry else len(plan.add)
    n_rep = sum(r.action == "replaced" for r in res.results) if not dry else len(plan.replace)
    for ds, fname in pending.items():
        notes.append(f"{ds}: {fname} stays in the PDF folder (the older edition too); run sync again to index it")
    failed_txt = f", nieudane {len(res.failed)}: {', '.join(res.failed)}" if res.failed else ""
    if res.cancelled:
        head = ", ".join(res.skipped[:8]) + (", …" if len(res.skipped) > 8 else "")
        summary = (
            f"Anulowano: dodano {n_add}, zastąpiono {n_rep}, usunięto {len(res.removed)}, "
            f"pominięto {len(res.skipped)}{f' ({head})' if res.skipped else ''}{failed_txt}"
        )
    else:
        summary = (
            f"{'Próba (dry run): ' if dry else ''}dodano {n_add}, zastąpiono {n_rep}, "
            f"usunięto {len(plan.remove)}, bez zmian {len(plan.unchanged)}{failed_txt}"
        )
    return {
        "summary": summary,
        "cancelled": res.cancelled,
        "skipped": res.skipped,
        "pdfs_pending": pending,
        "dry_run": dry,
        "scope": scope_ds,
        "plan": asdict(res.plan),
        "results": [r.to_dict() for r in res.results],
        "removed": res.removed,
        "failed": res.failed,
        "healed": {k: [asdict(m) for m in v] for k, v in res.healed.items()},
        "report": report,
        "report_path": res.report_path,
        "new_pdfs": new_files,
        "old_pdfs_deleted": retired,
        "pdfs_rejected": rejected,
        "source_item_ids_filled": filled,
        "notes": notes,
        "seconds": round(res.seconds, 1),
    }


RUNNERS: dict[str, Callable[[JobContext, dict[str, Any]], dict[str, Any]]] = {
    "sync": _run_sync,
    "check_citations": _run_check_citations,
    "catalog_refresh": _run_catalog_refresh,
}

__all__ = [
    "JobInfo",
    "NotCancellable",
    "cancel_job",
    "get_job",
    "latest_citations",
    "list_jobs",
    "start",
    "stop",
    "submit_job",
]
