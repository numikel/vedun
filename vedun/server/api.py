"""REST API (``/api``): JSON over the library in :mod:`vedun.search` plus job control.

Rules: library dataclasses are serialised with ``to_dict()``; nothing is re-implemented here. A lookup that
finds nothing is a normal answer (HTTP 200, ``found=false`` plus the nearest ids); an unknown document for
``toc`` / ``figures`` / ``documents/{doc_id}`` is a 404. Errors always use ``{"error": {"code", "message"}}``.
Evidence records get two extra fields for HTTP clients: ``figure_url`` and ``page_url`` (PNG endpoints below).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, StringConstraints

from vedun import catalog as K
from vedun import config as C
from vedun import documents as docs
from vedun import search as kb
from vedun.check_citations import check_citations
from vedun.embed import Embedder
from vedun.quote import verify_quote
from vedun.rerank import get_reranker

from . import budget, jobs
from . import settings as S
from .security import require_same_origin

router = APIRouter(prefix="/api")
SAME_ORIGIN = [Depends(require_same_origin)]

DsStr = Annotated[str, StringConstraints(pattern=S.SHEET_RE)]
KindStr = Literal["clause", "table", "figure", "appendix"]
DsPath = Annotated[str, PathParam(pattern=S.SHEET_RE, description="document id, e.g. 4-12")]
JOB_ID = r"^[0-9a-f]{12}$"
STATUSES = ("current", "outdated", "missing_locally", "withdrawn", "unknown")


def _budgeted_embedder(key: str) -> Embedder:
    """Query embedder whose library cost guard is what is left of today's budget."""
    return Embedder(C.MODELS[key], total_cost_guard=budget.remaining())


@dataclass
class Hooks:
    """Swap points so tests never touch the network."""

    embedder_factory: Any = _budgeted_embedder
    reranker_factory: Any = get_reranker


hooks = Hooks()


# ============================================================================ helpers
@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """A per-call read-only connection (sqlite objects are bound to the thread that created them)."""
    try:
        conn = S.open_readonly()
    except sqlite3.Error as e:
        raise HTTPException(503, f"knowledge base database unavailable: {e}") from e
    try:
        yield conn
    finally:
        conn.close()


def _decorate(o: Any) -> Any:
    """Add ``figure_url`` / ``page_url`` to every Evidence-shaped dict in a serialised response."""
    if isinstance(o, dict):
        if "unit_kind" in o and "doc_id" in o and "unit_id" in o:
            o["figure_url"] = (
                f"/api/figures/{o['doc_id']}/{quote(str(o['unit_id']))}.png" if o.get("figure_png") else None
            )
            o["page_url"] = f"/api/pages/{o['doc_id']}/{o['page_start']}.png" if o.get("page_start") else None
        for v in o.values():
            _decorate(v)
    elif isinstance(o, list):
        for v in o:
            _decorate(v)
    return o


def _dump(obj: Any) -> dict[str, Any]:
    return _decorate(obj.to_dict())


def _kind_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT doc_id, kind, COUNT(*) AS n FROM units WHERE dup_index=0 GROUP BY doc_id, kind"):
        out.setdefault(r["doc_id"], {})[r["kind"]] = r["n"]
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    err = {"code": code, "message": message, "status": status, **extra}
    return JSONResponse({"error": err, "detail": message}, status_code=status)


# ============================================================================ health + stats
@router.get("/health", tags=["service"], summary="Liveness (open: no authentication)")
def health() -> JSONResponse:
    """200 when the database opens. Deliberately minimal: no counts, no key names, no paths."""
    body: dict[str, Any] = {"status": "ok", "version": S.VERSION, "time": _now()}
    try:
        with db() as c:
            c.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
    except HTTPException:
        return JSONResponse({**body, "status": "error"}, status_code=503)
    return JSONResponse(body)


@router.get("/stats", tags=["service"], summary="Corpus, retrieval settings, catalog counts, API cost")
def stats() -> dict[str, Any]:
    """Counts of documents/units/chunks, effective retrieval defaults, catalog status counts and job cost."""
    with db() as c:
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        kinds = {r[0]: r[1] for r in c.execute("SELECT kind, COUNT(*) FROM units WHERE dup_index=0 GROUP BY kind")}
        corpus = {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "units": one("SELECT COUNT(*) FROM units WHERE dup_index=0"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "figures_with_image": one("SELECT COUNT(*) FROM units WHERE kind='figure' AND extra LIKE '%\"file\"%'"),
            "kinds": kinds,
            "last_indexed_at": c.execute("SELECT MAX(indexed_at) FROM documents").fetchone()[0],
        }
        cat = K.cached_catalog()
        rows = K.catalog_status(c, cat)
    js = jobs.jobs_summary()
    return {
        "corpus": corpus,
        "db_size_bytes": C.DB_PATH.stat().st_size if C.DB_PATH.exists() else None,
        "retrieval": {
            "w_fts": C.W_FTS,
            "w_vec": C.W_VEC,
            "rrf_k": C.RRF_K,
            "fusion": C.FUSION,
            "reranker": C.DEFAULT_RERANKER,
            "rerank_candidates": C.RERANK_CANDIDATES,
            "text_models": C.TEXT_MODEL_KEYS,
            "default_text_model": C.DEFAULT_TEXT_MODEL,
            "default_image_model": C.DEFAULT_IMAGE_MODEL,
        },
        "catalog": K.counts_by_status(rows) if cat else {},  # flat status -> n (the dashboard reads it so)
        "catalog_fetched_at": cat.fetched_at if cat else None,
        "jobs": js["by_status"],
        "cost_usd": js["cost_usd"],  # API cost of finished jobs (sync embeddings); query costs are not summed
        "budget_usd": S.daily_budget_usd(),
        "spent_today_usd": round(budget.spent_today(), 6),
        "last_sync": js["last_sync"] or corpus["last_indexed_at"],
    }


# ============================================================================ collection
def _collection_items(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], K.Catalog | None]:
    cat = K.cached_catalog()
    infos = {s.doc_id: s for s in kb.list_documents(conn)}
    kinds = _kind_counts(conn)
    items = []
    for st in K.catalog_status(conn, cat):
        d = st.to_dict()
        d["catalog_status"] = st.status
        d["local"] = st.local_pdf is not None
        info = infos.get(st.doc_id)
        if info:
            d.update(
                title=info.title,
                edition=info.edition,
                edition_label=info.edition_label,
                pages=info.pages,
                n_units=info.n_units,
                n_chunks=info.n_chunks,
                n_figures=info.n_figures,
                indexed_at=info.indexed_at,
                models=info.models,
                kinds=kinds.get(st.doc_id, {}),
            )
        items.append(d)
    return items, cat


@router.get("/collection", tags=["collection"], summary="All documents: indexed, on disk and in the source catalog")
def collection(
    q: Annotated[str | None, Query(max_length=100, description="substring of number or title")] = None,
    status: Annotated[Literal["current", "outdated", "missing_locally", "withdrawn", "unknown"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    with db() as c:
        items, cat = _collection_items(c)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    if q:
        ql = q.lower()
        items = [i for i in items if ql in f"{i['doc_id']} {i.get('title') or ''}".lower()]
    if status:
        items = [i for i in items if i["status"] == status]
    return {
        "documents": items[offset : offset + limit],
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(items),
        "counts": counts,
        "catalog_fetched_at": cat.fetched_at if cat else None,
    }


@router.get(
    "/collection/{doc_id}", tags=["collection"], summary="One document: index info, catalog status, unit counts"
)
def collection_item(doc_id: DsPath) -> dict[str, Any]:
    with db() as c:
        items, _ = _collection_items(c)
    it = next((i for i in items if i["doc_id"] == doc_id), None)
    if it is None:
        raise HTTPException(404, f"{C.doc_label(doc_id)} is neither indexed, on disk nor in the cached catalog")
    return it


@router.get("/toc/{doc_id}", tags=["retrieval"], summary="Structure of a document or branch (no text)")
def toc(
    doc_id: DsPath,
    prefix: Annotated[str | None, Query(max_length=60)] = None,
    depth: Annotated[int | None, Query(ge=1, le=12)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    with db() as c:
        r = kb.toc(c, doc_id, prefix, depth, limit, offset)
    if r.document_title is None:
        raise HTTPException(404, r.message)
    return _dump(r)


@router.get("/lookup", tags=["retrieval"], summary="Exact lookup by document + clause/table/figure id")
def lookup(
    doc_id: Annotated[str, Query(pattern=S.SHEET_RE)],
    ref: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=80)]], Query(max_length=20)],
    children: bool = False,
    neighbors: bool = False,
    max_chars: Annotated[int | None, Query(ge=100, le=200_000)] = None,
) -> dict[str, Any]:
    """``ref`` is repeatable and forgiving (``2.3.1.4``, ``pkt 2.3.1.4``, ``Tab. 2.4.1.1``)."""
    with db() as c:
        r = kb.lookup(c, doc_id, ref, include_children=children, neighbors=neighbors, max_chars=max_chars)
    out = _dump(r)
    out["found"] = r.found
    return out


@router.get("/fulltext", tags=["retrieval"], summary="FTS5/BM25 only (no vectors, no network)")
def fulltext(
    q: Annotated[str, Query(min_length=1, max_length=500)],
    doc_id: Annotated[list[DsStr] | None, Query(max_length=20)] = None,
    kind: Annotated[list[KindStr] | None, Query(max_length=4)] = None,
    syntax: Literal["safe", "fts5"] = "safe",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> Any:
    with db() as c:
        r = kb.fulltext(c, q, doc_id, kind, limit, offset, syntax)
    if not r.ok:
        return _error(400, "invalid_query", r.error or "invalid query")
    return _dump(r)


@router.get(
    "/search",
    tags=["retrieval"],
    dependencies=SAME_ORIGIN,  # spends money (embedding, rerank): same cross-site guard as a POST
    summary="Hybrid search: weighted RRF of BM25 + vectors, optional reranker",
)
def search(
    q: Annotated[str, Query(min_length=1, max_length=500)],
    doc_id: Annotated[list[DsStr] | None, Query(max_length=20)] = None,
    kind: Annotated[list[KindStr] | None, Query(max_length=4)] = None,
    w_fts: Annotated[float | None, Query(ge=0, le=1)] = None,
    w_vec: Annotated[float | None, Query(ge=0, le=1)] = None,
    rerank: Annotated[
        Literal["default", "none", "r1", "r2", "r3"], Query(description="default = configured reranker")
    ] = "default",
    include_figures: bool = True,
    mode: Literal["hybrid", "bm25", "vector"] = "hybrid",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0, le=1000)] = 0,
) -> dict[str, Any]:
    """A failed or unconfigured reranker never fails the request: the fused order is returned with
    ``rerank_error`` set; so is a failed vector retrieval (BM25 only, see ``notes``)."""
    over_budget = budget.exhausted()
    if over_budget:  # no embedding, no rerank: BM25 only, and say so
        mode, rerank, include_figures = "bm25", "none", False
    with db() as c:
        try:
            r = kb.search(
                c,
                q,
                doc_id,
                kind,
                w_fts=w_fts,
                w_vec=w_vec,
                rerank=None if rerank == "none" else rerank,
                include_figures=include_figures,
                limit=limit,
                offset=offset,
                mode=mode,
                embedder_factory=lambda k: hooks.embedder_factory(k),
                reranker_factory=lambda k: hooks.reranker_factory(k),
            )
        except Exception as e:  # noqa: BLE001 - mode="vector" re-raises embedding errors
            return _error(502, "retrieval_failed", f"{type(e).__name__}: {str(e)[:300]}")
    budget.flush()
    if over_budget:
        r.notes.append(
            f"{budget.EXHAUSTED_MESSAGE} ({S.daily_budget_usd():.2f} USD): BM25 only, no embeddings, no reranking"
        )
    out = _dump(r)
    out["budget_exhausted"] = over_budget
    err = next((h.retrieval.rerank_error for h in r.hits if h.retrieval.rerank_error), None)
    err = err or next((n for n in r.notes if n.startswith("reranker")), None)
    out["rerank_error"] = err
    out["reranked"] = bool(r.reranker) and err is None and any(h.retrieval.rerank for h in r.hits)
    return out


# ============================================================================ verification
class VerifyIn(BaseModel):
    doc_id: str = Field(pattern=S.SHEET_RE)
    ref: str = Field(min_length=1, max_length=80)
    quote: str = Field(min_length=1, max_length=5000)


class CitationsIn(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)


@router.post(
    "/verify",
    tags=["verification"],
    dependencies=SAME_ORIGIN,
    summary="Verify a quote against the unit and the PDF text layer",
)
def verify(body: VerifyIn) -> dict[str, Any]:
    with db() as c:
        r = verify_quote(c, body.doc_id, body.ref, body.quote)
    return _dump(r)


@router.post(
    "/check-citations", tags=["verification"], dependencies=SAME_ORIGIN, summary="Validate document citations in a text"
)
def check_citations_text(body: CitationsIn) -> dict[str, Any]:
    with db() as c:
        res = check_citations(c, body.text)
    rows = [r.to_dict() for r in res]
    return {
        "results": rows,
        "counts": {s: sum(r["status"] == s for r in rows) for s in ("OK", "UNKNOWN UNIT", "DOCUMENT NOT INDEXED")},
    }


@router.get("/citations/latest", tags=["verification"], summary="Results of the last check-citations job")
def citations_latest() -> dict[str, Any]:
    r = jobs.latest_citations()
    if r is None:
        raise HTTPException(404, "no check-citations job has finished yet")
    return r


# ============================================================================ whole documents
def _doc_errors(fn: Any, *a: Any, **k: Any) -> Any:
    try:
        return fn(*a, **k)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/documents", tags=["documents"], summary="Documents with an original PDF and/or Markdown, with host paths")
def documents_list() -> dict[str, Any]:
    with db() as c:
        items = docs.list_documents(c)
    return {"documents": items, "total": len(items)}


@router.get("/documents/{doc_id}.md", tags=["documents"], summary="Markdown of a document or one section (paginated)")
def document_md(
    doc_id: DsPath,
    section: Annotated[
        str | None, Query(max_length=60, description="clause id, e.g. 2.3.4 (with everything under it)")
    ] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    max_chars: Annotated[int, Query(ge=1000, le=docs.MAX_PAGE_CHARS)] = 60_000,
    full: Annotated[bool, Query(description="whole text as a download, no pagination")] = False,
) -> Any:
    """The text is in the body; pagination is in headers ``X-Total-Chars``, ``X-Next-Offset`` (empty on the last page)."""
    with db() as c:
        d = _doc_errors(
            docs.get_document, c, doc_id, "md", section, 0 if full else offset, 10**9 if full else max_chars
        )
    if not d["found"]:
        raise HTTPException(404, d.get("message") or f"no Markdown for {C.doc_label(doc_id)}")
    headers = {
        "X-Total-Chars": str(d["total_chars"]),
        "X-Next-Offset": "" if d["next_offset"] is None else str(d["next_offset"]),
    }
    if full:
        headers["Content-Disposition"] = f'attachment; filename="{doc_id}.md"'
    return PlainTextResponse(d["text"], media_type="text/markdown; charset=utf-8", headers=headers)


@router.get("/documents/{doc_id}.pdf", tags=["documents"], summary="The original PDF")
def document_pdf(doc_id: DsPath) -> FileResponse:
    with db() as c:
        p = _doc_errors(docs.pdf_file, doc_id, c)
    if p is None:
        raise HTTPException(404, f"no PDF for {C.doc_label(doc_id)}")
    return FileResponse(p, media_type="application/pdf", filename=p.name, content_disposition_type="inline")


@router.get("/documents/{doc_id}", tags=["documents"], summary="Metadata, top-level structure and host/container paths")
def document_meta(doc_id: DsPath) -> dict[str, Any]:
    with db() as c:
        d = _doc_errors(docs.get_document, c, doc_id, "meta")
    if not d["found"]:
        raise HTTPException(404, d["message"])
    return d


# ============================================================================ images
@router.get("/figures/{doc_id}/{figure_id:path}", tags=["images"], summary="PNG crop of a figure")
def figure_png(doc_id: DsPath, figure_id: str) -> FileResponse:
    if len(figure_id) > 80:
        raise HTTPException(404, "no such figure")
    with db() as c:
        r = kb.figure(c, doc_id, figure_id.removesuffix(".png"))
    if not r.ok or not r.path or not Path(r.path).is_file():
        raise HTTPException(404, r.message or "no image for this figure")
    return FileResponse(r.path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/pages/{doc_id}/{page}.png", tags=["images"], summary="PNG of a PDF page (rendered on demand)")
def page_png(doc_id: DsPath, page: Annotated[int, PathParam(ge=1, le=5000)]) -> FileResponse:
    with db() as c:
        r = kb.page_image(c, doc_id, page)
    if not r.ok or not r.path or not Path(r.path).is_file():
        raise HTTPException(404, r.message or "no such page")
    return FileResponse(r.path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


# ============================================================================ jobs
class CheckCitationsJobIn(BaseModel):
    repo: str | None = Field(default=None, max_length=500, description="folder inside the repo folder; default = all")


def _accepted(job_id: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"id": job_id, "job_id": job_id, "status_url": f"/api/jobs/{job_id}", **extra}, status_code=202)


@router.post(
    "/jobs/check-citations",
    tags=["jobs"],
    dependencies=SAME_ORIGIN,
    status_code=202,
    summary="Scan the repo for document citations",
)
def job_check_citations(body: CheckCitationsJobIn | None = None) -> JSONResponse:
    repo = body.repo if body else None
    try:
        jobs.resolve_repo(repo)  # fail fast with a clear message instead of a failed job
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return _accepted(jobs.submit_job("check_citations", {"repo": repo} if repo else {}))


@router.post(
    "/jobs/catalog-refresh",
    tags=["jobs"],
    dependencies=SAME_ORIGIN,
    status_code=202,
    summary="Re-read the source catalog",
)
def job_catalog_refresh() -> JSONResponse:
    return _accepted(jobs.submit_job("catalog_refresh", {}))


@router.post(
    "/jobs/sync",
    tags=["jobs"],
    dependencies=SAME_ORIGIN,
    status_code=202,
    summary="Index uploaded and/or newly downloaded PDFs",
)
async def job_sync(
    request: Request,
    files: Annotated[list[UploadFile] | None, File(description="PDFs named <doc_id>_<YYYYMM>.pdf")] = None,
    fetch_from_source: Annotated[bool, Form()] = False,
    only: Annotated[
        str, Form(description="space or comma separated document numbers; default = indexed documents")
    ] = "",
    include_missing: Annotated[bool, Form()] = False,
    dry_run: Annotated[bool, Form()] = False,
    force: Annotated[bool, Form()] = False,
    no_embed: Annotated[bool, Form()] = False,
) -> JSONResponse:
    """Scope is never wider than ``only`` or, by default, the already indexed documents plus the new files."""
    limit = S.max_upload_bytes()
    files = [f for f in (files or []) if f.filename]
    if len(files) > 10:
        raise HTTPException(400, "at most 10 PDFs per request")
    clen = int(request.headers.get("content-length") or 0)
    if clen > limit * max(1, len(files)) + 1_000_000:
        raise HTTPException(413, f"request too large (limit {limit // 1_000_000} MB per PDF)")
    only_list = [s for s in re.split(r"[\s,;]+", only.strip()) if s]
    bad = [s for s in only_list if not re.fullmatch(S.SHEET_RE, s)]
    if bad:
        raise HTTPException(400, f"bad document number(s) in 'only': {bad}")
    names = [Path(f.filename or "").name for f in files]
    badn = [n for n in names if not re.fullmatch(S.PDF_NAME_RE, n)]
    if badn:
        raise HTTPException(400, f"file name must look like 4-12_202610.pdf (got {badn})")
    if len(set(names)) != len(names):
        raise HTTPException(400, "duplicate file names in one request")
    staged: list[Path] = []
    try:
        dest = S.incoming_dir()
        dest.mkdir(parents=True, exist_ok=True)
        for f, name in zip(files, names, strict=True):
            target = dest / name
            staged.append(target)
            size = 0
            with target.open("wb") as fh:
                head = await f.read(1024)
                if not head.startswith(b"%PDF-"):
                    raise HTTPException(400, f"{name}: not a PDF (missing %PDF- header)")
                chunk = head
                while chunk:
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"{name}: larger than {limit // 1_000_000} MB")
                    fh.write(chunk)
                    chunk = await f.read(1 << 20)
        job_id = jobs.submit_job(
            "sync",
            {
                "uploaded": names,
                "fetch_from_source": fetch_from_source,
                "only": only_list,
                "include_missing": include_missing,
                "dry_run": dry_run,
                "force": force,
                "no_embed": no_embed,
            },
        )
    except BaseException:
        for p in staged:
            p.unlink(missing_ok=True)
        raise
    return _accepted(job_id, uploaded=names)


@router.get("/jobs", tags=["jobs"], summary="Job history, newest first")
def list_jobs(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    return {"jobs": [j.to_dict() for j in jobs.list_jobs(limit)]}


@router.get("/jobs/{job_id}", tags=["jobs"], summary="One job (poll this)")
def get_job(job_id: Annotated[str, PathParam(pattern=JOB_ID)]) -> dict[str, Any]:
    try:
        return jobs.get_job(job_id).to_dict()
    except KeyError:
        raise HTTPException(404, f"no job {job_id}") from None


@router.post(
    "/jobs/{job_id}/cancel",
    tags=["jobs"],
    dependencies=SAME_ORIGIN,
    summary="Cancel a queued job now, or ask a running one to stop at its next safe point",
    responses={
        200: {"description": "Queued job: status is now `cancelled`."},
        202: {"description": "Running job: `cancel_requested` is true; poll the job until it is `cancelled`."},
        409: {"description": "Already finished, or a running job of a kind with no safe point."},
    },
)
def cancel_job(job_id: Annotated[str, PathParam(pattern=JOB_ID)]) -> JSONResponse:
    """A sync stops between documents: finished documents stay finished, the rest are listed in ``result.skipped``."""
    try:
        job = jobs.cancel_job(job_id)
    except KeyError:
        raise HTTPException(404, f"no job {job_id}") from None
    except jobs.NotCancellable as e:
        raise HTTPException(409, str(e)) from None
    return JSONResponse(job.to_dict(), status_code=202 if job.status == "running" else 200)


# ============================================================================ reports
def _report_files() -> list[Path]:
    d = C.REPORT_DIR
    if not d.is_dir():
        return []
    return sorted(
        (p for p in d.iterdir() if p.is_file() and re.fullmatch(S.REPORT_NAME_RE, p.name)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


@router.get("/reports", tags=["reports"], summary="Sync reports and evaluation files")
def reports() -> dict[str, Any]:
    out = []
    for p in _report_files():
        st = p.stat()
        out.append(
            {
                "name": p.name,
                "bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
            }
        )
    return {"reports": out}


@router.get("/reports/{name}", tags=["reports"], summary="One report (Markdown or JSON)")
def report(name: Annotated[str, PathParam(pattern=S.REPORT_NAME_RE)]) -> Any:
    p = next((x for x in _report_files() if x.name == name), None)  # matched against the listing: no path building
    if p is None:
        raise HTTPException(404, f"no report {name}")
    if p.suffix == ".json":
        return FileResponse(p, media_type="application/json")
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"), media_type="text/markdown; charset=utf-8")
