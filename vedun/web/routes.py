"""HTML pages of the knowledge base (server-rendered Jinja2 + HTMX).

``router`` is meant to be included by the application (``app.include_router(router)``) together
with a ``/static`` mount pointing at :data:`STATIC_DIR`. Pages read the library directly
(``vedun.search``); everything optional (jobs, catalog, citations) degrades to an empty state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from vedun import catalog as K
from vedun import config as C
from vedun import search as kb
from vedun.quote import verify_quote

from . import data as W
from . import i18n as I
from . import mcp_setup
from . import reports as R
from .i18n import N_, t
from .rendering import highlight, marked_terms, render_text, snippet_html

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
router = APIRouter(include_in_schema=False, dependencies=[Depends(I.activate)])
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

NAV = [
    ("overview", "/", N_("Overview"), "layout-dashboard"),
    ("documents", "/documents", N_("Documents"), "library"),
    ("search", "/search", N_("Search"), "search"),
    ("sync", "/sync", N_("Sync"), "refresh"),
    ("citations", "/citations", N_("Citations in repo"), "file-check"),
    ("connect", "/connect", N_("MCP connection"), "plug"),
]
MODES = [("lookup", N_("By number")), ("fulltext", N_("Full text")), ("hybrid", N_("Hybrid"))]
#: reference prefixes the lookup box accepts (input tokens of the corpus, not translated UI text)
LOOKUP_PREFIXES = "pkt, punkt, Section, Tab., Table, rys., Fig., zał."
PAGE_SIZE = 10
CITATIONS_PAGE = 100


# ====================================================================== template environment
def static_url(path: str) -> str:
    try:
        v = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        v = 0
    return f"/static/{path}?v={v}"


def fig_url(ds: str, unit_id: str) -> str:
    return f"/media/figures/{quote(ds)}/{quote(unit_id)}.png"


def page_url(ds: str, page: int) -> str:
    return f"/media/pages/{quote(ds)}/{int(page)}.png"


def fmt_score(v: Any, digits: int = 3) -> str:
    return "—" if v is None else W.decimal(f"{float(v):.{digits}f}")


def ranker_label(r: str | None) -> str:
    """Human description of ``final_ranker`` (``wrrf+rerank:r3``)."""
    if not r:
        return "—"
    parts = []
    for p in r.split("+"):
        if p == "exact":
            parts.append(t("exact number"))
        elif p == "wrrf":
            parts.append(t("weighted RRF fusion (BM25 + vectors)"))
        elif p == "bm25":
            parts.append("BM25")
        elif p.startswith("vector"):
            parts.append(t("vectors"))
        elif p.startswith("rerank:"):
            parts.append("reranker " + W.RERANK_NAME.get(p.split(":", 1)[1], p.split(":", 1)[1]))
        else:
            parts.append(p)
    return ", ".join(parts)


def note_lead(note: str) -> tuple[str, str]:
    """Localized lead for a degradation note of ``search`` plus the raw technical detail."""
    low = note.lower()
    if low.startswith("vector retrieval unavailable"):
        return t("Vector search is unavailable, results come from BM25 only."), note.split(":", 1)[-1].strip()
    if low.startswith("figure retrieval unavailable"):
        return t("Searching figures by image is unavailable."), note.split(":", 1)[-1].strip()
    if low.startswith("reranker"):
        return t("The reranker failed, the order from RRF fusion was kept."), note.split(":", 1)[-1].strip()
    if low.startswith(("document(s)", "sheet(s)")):
        return t("A document named in the query is not indexed."), note
    return note, ""


def pretty(v: Any) -> str:
    """Compact JSON dump of a job result for the details box (empty when there is nothing to show)."""
    if v in (None, "", [], {}):
        return ""
    try:
        out = json.dumps(v, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        out = str(v)
    return out if len(out) <= 6000 else out[:6000] + "\n…"


def _highlighted(ev: Any) -> Any:
    """Verbatim evidence text rendered with the FTS5 highlight terms of its snippet."""
    return render_text(ev.text, marked_terms(ev.snippet))


PLUGIN_GUIDE_URL = "https://github.com/numikel/vedun/blob/main/docs/reference.md#writing-a-source-plugin"


def source_label() -> str:
    """Name of the configured document source for page wording; a neutral phrase when there is none."""
    return K.source_name() or t("the source")


def _install_env() -> None:
    env = templates.env
    env.globals.update(
        NAV=NAV,
        static=static_url,
        fig_url=fig_url,
        page_url=page_url,
        KIND_LABEL=W.KIND_LABEL,
        KIND_ICON=W.KIND_ICON,
        STATUS_META=W.STATUS_META,
        JOB_STATUS_META=W.JOB_STATUS_META,
        RERANK_NAME=W.RERANK_NAME,
        url_with=W.url_with,
        _=t,
        source_name=source_label,
        has_source=lambda: K.source_name() is not None,
        PLUGIN_GUIDE_URL=PLUGIN_GUIDE_URL,
        corpus_name=lambda: C.CORPUS_NAME,
        doc_label=C.doc_label,
        examples=W.search_examples,
        LOOKUP_PREFIXES=LOOKUP_PREFIXES,
        LANG_NAMES=I.LANG_NAMES,
        UI_LANGUAGES=C.UI_LANGUAGES,
    )
    env.filters.update(
        dt=W.fmt_dt,
        dts=lambda v: W.fmt_dt(v, seconds=True),
        num=W.fmt_int,
        usd=W.fmt_usd,
        bytes=W.fmt_bytes,
        score=fmt_score,
        dec=W.decimal,
        ranker=ranker_label,
        evtext=_highlighted,
        snip=snippet_html,
        pretty=pretty,
        note_lead=note_lead,
        plural=I.plural,
        duration=W.fmt_duration,
        hl=lambda s, snip=None: highlight(s or "", marked_terms(snip)),
    )


_install_env()


def lang_url(request: Request, code: str) -> str:
    """The current URL with ``?lang=code`` (all other query parameters kept) for the header switch."""
    items = [(k, v) for k, v in request.query_params.multi_items() if k != I.QUERY_PARAM]
    return f"{request.url.path}?{urlencode([*items, (I.QUERY_PARAM, code)])}"


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    lang = I.current()
    ctx.setdefault("active", "")
    ctx.update(
        lang=lang,
        lang_links={code: lang_url(request, code) for code in C.UI_LANGUAGES},
        js_i18n=I.js_payload(lang),
    )
    resp = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    resp.headers["Vary"] = "HX-Request, HX-Target, Cookie"
    I.remember(request, resp, lang)
    return resp


def hx_target(request: Request) -> str:
    """Id of the element HTMX will swap (empty for full-page navigation and history restores)."""
    if request.headers.get("HX-History-Restore-Request"):
        return ""
    return request.headers.get("HX-Target", "") if request.headers.get("HX-Request") == "true" else ""


def require_htmx(request: Request) -> None:
    """State-changing routes only accept HTMX requests (a custom header blocks cross-site forms)."""
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(400, t("An HTMX request is required."))


async def form_body(request: Request) -> dict[str, str]:
    """Urlencoded form body without needing python-multipart."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def not_found(request: Request, title: str, text: str) -> HTMLResponse:
    return render(request, "error.html", status_code=404, title=title, text=text)


def _float(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(str(v).replace(",", "."))))
    except (TypeError, ValueError):
        return default


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# ====================================================================== overview
def _newest(*values: Any) -> Any:
    best = None
    for v in values:
        d = W.to_dt(v)
        if d is not None and (best is None or d > best[0]):
            best = (d, v)
    return best[1] if best else None


@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    with W.open_db() as conn:
        sheets = kb.list_documents(conn)
        counts = W.kind_counts(conn)
    totals = {k: sum(c.get(k, 0) for c in counts.values()) for k in ("clause", "table", "figure", "appendix")}
    cat = W.catalog_info(request.app)
    stats = W.stats_info(request.app)
    reports = R.list_reports(C.REPORT_DIR)
    jobs = W.list_jobs(8)
    last_job = next((j for j in jobs if j.kind == "sync" and j.status == "done"), None)
    last_sync = _newest(
        stats.get("last_sync"),
        last_job.finished_at if last_job else None,
        reports[0].mtime if reports else None,
        *(s.indexed_at for s in sheets),
    )
    cat_counts = cat.counts or (
        {W.norm_status(k): v for k, v in stats["catalog"].items()} if isinstance(stats.get("catalog"), dict) else {}
    )
    return render(
        request,
        "overview.html",
        active="overview",
        sheets=sorted(sheets, key=lambda s: W.dotted_key(s.doc_id)),
        totals=totals,
        cat=cat,
        cat_counts=cat_counts,
        cost=stats.get("cost_usd"),
        last_sync=last_sync,
        reports=reports[:5],
        jobs=jobs[:6],
        jobs_ok=W.jobs_available(),
        report_dir=str(C.REPORT_DIR),
    )


# ====================================================================== documents
SORTS = {
    "ds": lambda r: W.dotted_key(r["ds"]),
    "title": lambda r: (r["title"] or "").lower(),
    "edition": lambda r: r["edition"] or "",
    "clause": lambda r: r["clause"],
    "table": lambda r: r["table"],
    "figure": lambda r: r["figure"],
    "status": lambda r: W.STATUS_ORDER.index(r["status"]) if r["status"] in W.STATUS_ORDER else 9,
}


def document_rows(conn: Any, cat: W.CatalogInfo) -> list[dict[str, Any]]:
    counts = W.kind_counts(conn)
    rows: list[dict[str, Any]] = []
    local = set()
    for s in kb.list_documents(conn):
        local.add(s.doc_id)
        c = counts.get(s.doc_id, {})
        extra = cat.by_ds.get(s.doc_id, {})
        rows.append(
            {
                "ds": s.doc_id,
                "title": s.title,
                "edition": s.edition,
                "edition_label": s.edition_label or s.edition,
                "pages": s.pages,
                "clause": c.get("clause", 0),
                "table": c.get("table", 0),
                "figure": c.get("figure", 0),
                "status": extra.get("status", "unknown"),
                "latest": extra.get("latest"),
                "local": True,
            }
        )
    for ds, extra in cat.by_ds.items():
        if ds not in local:
            rows.append(
                {
                    "ds": ds,
                    "title": extra.get("title") or "",
                    "edition": "",
                    "edition_label": "",
                    "pages": 0,
                    "clause": 0,
                    "table": 0,
                    "figure": 0,
                    "status": extra.get("status", "unknown"),
                    "latest": extra.get("latest"),
                    "local": False,
                }
            )
    return rows


@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request) -> HTMLResponse:
    qp = request.query_params
    q = (qp.get("q") or "").strip()
    status = qp.get("status", "")
    sort = qp.get("sort", "ds") if qp.get("sort", "ds") in SORTS else "ds"
    desc = qp.get("dir") == "desc"
    cat = W.catalog_info(request.app)
    with W.open_db() as conn:
        rows = document_rows(conn, cat)
    total = len(rows)
    status_counts = {k: sum(1 for r in rows if r["status"] == k) for k in W.STATUS_META}
    terms = q.lower().split()
    rows = [r for r in rows if all(t in f"{r['ds']} {r['title']}".lower() for t in terms)]
    if status in W.STATUS_META:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(key=SORTS[sort], reverse=desc)
    params = {"q": q, "status": status, "sort": sort, "dir": "desc" if desc else ""}
    sort_urls = {
        col: W.url_with("/documents", params, sort=col, dir="desc" if (sort == col and not desc) else "")
        for col in SORTS
    }
    ctx = dict(
        rows=rows,
        total=total,
        q=q,
        status=status,
        sort=sort,
        desc=desc,
        sort_urls=sort_urls,
        status_counts=status_counts,
        cat_available=cat.available,
    )
    if hx_target(request) == "documents-table":
        return render(request, "partials/documents_table.html", **ctx)
    return render(request, "documents.html", active="documents", **ctx)


def build_toc(conn: Any, ds: str, open_ids: list[str], active: str | None) -> list[dict[str, Any]]:
    """Top-level TOC nodes; the branch along ``open_ids`` is expanded, everything else is lazy."""

    def level(prefix: str | None) -> list[dict[str, Any]]:
        r = kb.toc(conn, ds, prefix, depth=1 if prefix else 0, limit=500)
        want = 1 if prefix else 0
        return [
            {
                "unit_id": i.unit_id,
                "kind": i.kind,
                "title": i.title,
                "n_children": i.n_children,
                "children": None,
                "active": i.unit_id == active,
            }
            for i in r.items
            if i.depth == want
        ]

    top = level(None)
    cur = top
    for oid in open_ids:
        node = next((n for n in cur if n["unit_id"] == oid), None)
        if node is None:
            break
        node["children"] = level(oid)
        cur = node["children"]
    return top


@router.get("/documents/{ds}", response_class=HTMLResponse)
def document_detail(request: Request, ds: str) -> HTMLResponse:
    qp = request.query_params
    unit = (qp.get("unit") or "").strip()
    tab = "figures" if qp.get("tab") == "figures" else "toc"
    cat = W.catalog_info(request.app)
    with W.open_db() as conn:
        info = next((s for s in kb.list_documents(conn) if s.doc_id == ds), None)
        if info is None:
            return not_found(
                request, t("No such document"), t("{doc} is not indexed in this database.", doc=C.doc_label(ds))
            )
        counts = W.kind_counts(conn).get(ds, {})
        item = None
        open_ids: list[str] = []
        if unit:
            item = kb.lookup(conn, ds, unit, neighbors=True, include_children=True).items[0]
            if item.found and item.evidence:
                ev = item.evidence
                open_ids = [p["unit_id"] for p in ev.parent_path]
                if ev.unit_kind == "clause":
                    open_ids.append(ev.unit_id)
        tree = build_toc(conn, ds, open_ids, item.evidence.unit_id if item and item.found and item.evidence else None)
        figures = W.figure_rows(conn, ds) if tab == "figures" else []
    extra = cat.by_ds.get(ds, {})
    try:  # original PDF / Markdown links and host paths (optional integration)
        from vedun.documents import paths_for

        doc = paths_for(ds, info.source_pdf, info.edition)
    except Exception:  # noqa: BLE001
        doc = None
    return render(
        request,
        "document_detail.html",
        active="documents",
        doc=doc,
        s=info,
        counts=counts,
        status=extra.get("status", "unknown"),
        latest=extra.get("latest"),
        tree=tree,
        item=item,
        tab=tab,
        figures=figures,
        unit=unit,
    )


@router.get("/documents/{ds}/toc", response_class=HTMLResponse)
def document_toc(request: Request, ds: str) -> HTMLResponse:
    prefix = (request.query_params.get("prefix") or "").strip() or None
    with W.open_db() as conn:
        nodes = build_toc(conn, ds, [], None) if prefix is None else _children_of(conn, ds, prefix)
    return render(request, "partials/toc_nodes.html", nodes=nodes, ds=ds)


def _children_of(conn: Any, ds: str, prefix: str) -> list[dict[str, Any]]:
    r = kb.toc(conn, ds, prefix, depth=1, limit=500)
    return [
        {
            "unit_id": i.unit_id,
            "kind": i.kind,
            "title": i.title,
            "n_children": i.n_children,
            "children": None,
            "active": False,
        }
        for i in r.items
        if i.depth == 1
    ]


@router.get("/documents/{ds}/unit", response_class=HTMLResponse)
def document_unit(request: Request, ds: str) -> HTMLResponse:
    ref = (request.query_params.get("ref") or "").strip()
    with W.open_db() as conn:
        resp = kb.lookup(conn, ds, ref, neighbors=True, include_children=True)
    return render(
        request,
        "partials/lookup_item.html",
        item=resp.items[0],
        ds=ds,
        viewer=True,
        document_known=resp.document_indexed,
    )


# ====================================================================== search
def _search_form(request: Request) -> dict[str, Any]:
    qp = request.query_params
    mode = qp.get("mode", "hybrid")
    rr = qp.get("rerank", C.DEFAULT_RERANKER or "none")
    kind = qp.get("kind", "")
    return {
        "mode": mode if mode in dict(MODES) else "hybrid",
        "q": (qp.get("q") or "").strip(),
        "ds": (qp.get("ds") or "").strip(),
        "ref": (qp.get("ref") or "").strip(),
        "kind": kind if kind in W.KIND_LABEL else "",
        "syntax": "fts5" if qp.get("syntax") == "fts5" else "safe",
        "w_fts": _float(qp.get("w_fts"), 0.4, 0.0, 1.0),
        "rerank": rr if rr in dict(W.RERANKERS) else "none",
        "figures": (qp.getlist("figures") or ["1"])[-1] == "1",
        "children": qp.get("children") == "1",
        "neighbors": qp.get("neighbors") == "1",
        "offset": _int(qp.get("offset"), 0, 0, 100000),
    }


def _pager(request: Request, form: dict[str, Any], shown: int, total: int | None, has_more: bool) -> dict[str, Any]:
    params = {k: v for k, v in dict(request.query_params).items()}
    off = form["offset"]
    return {
        "first": off + 1 if shown else 0,
        "last": off + shown,
        "total": total,
        "prev": W.url_with("/search", params, offset=max(0, off - PAGE_SIZE) or "") if off > 0 else None,
        "next": W.url_with("/search", params, offset=off + PAGE_SIZE) if has_more else None,
    }


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request) -> HTMLResponse:
    form = _search_form(request)
    mode = form["mode"]
    result: dict[str, Any] | None = None
    with W.open_db() as conn:
        sheets = sorted(kb.list_documents(conn), key=lambda s: W.dotted_key(s.doc_id))
        try:
            if mode == "lookup" and form["ref"]:
                ds, refs = form["ds"], [r.strip() for r in re.split(r"[;\n]+", form["ref"]) if r.strip()]
                if not ds:  # "4-12 pkt 2.3.1" typed into the ref box
                    dss, ids = kb.route_query(form["ref"])
                    if dss:
                        ds, refs = dss[0], ids or refs
                if not ds:
                    result = {
                        "kind": "error",
                        "message": t("Enter a document number (e.g. 4-12) or type it together with the clause number."),
                    }
                else:
                    resp = kb.lookup(conn, ds, refs, include_children=form["children"], neighbors=form["neighbors"])
                    result = {"kind": "lookup", "resp": resp, "ds": ds}
            elif mode == "fulltext" and form["q"]:
                resp = kb.fulltext(
                    conn,
                    form["q"],
                    ds=form["ds"] or None,
                    kind=form["kind"] or None,
                    limit=PAGE_SIZE,
                    offset=form["offset"],
                    syntax=form["syntax"],
                )
                result = {
                    "kind": "fulltext",
                    "resp": resp,
                    "hits": resp.hits,
                    "pager": _pager(request, form, len(resp.hits), resp.total, resp.has_more),
                }
            elif mode == "hybrid" and form["q"]:
                w = round(form["w_fts"], 3)
                resp = kb.search(
                    conn,
                    form["q"],
                    ds=form["ds"] or None,
                    kind=form["kind"] or None,
                    w_fts=w,
                    w_vec=round(1 - w, 3),
                    rerank=None if form["rerank"] == "none" else form["rerank"],
                    include_figures=form["figures"],
                    limit=PAGE_SIZE,
                    offset=form["offset"],
                    embedder_factory=W.hooks.embedder_factory,
                    reranker_factory=W.hooks.reranker_factory,
                )
                result = {
                    "kind": "hybrid",
                    "resp": resp,
                    "hits": resp.hits,
                    "pager": _pager(request, form, len(resp.hits), resp.total_candidates, resp.has_more),
                }
        except Exception as e:  # noqa: BLE001 - show the failure on the page instead of a 500
            result = {
                "kind": "error",
                "message": t("Search failed: {error}", error=f"{type(e).__name__}: {str(e)[:300]}"),
            }
    ctx = dict(form=form, result=result, sheets=sheets, modes=MODES, rerankers=W.RERANKERS, kind_options=W.KIND_OPTIONS)
    target = hx_target(request)
    if target == "results":
        return render(request, "partials/search_results.html", **ctx)
    if target == "search-panel":
        return render(request, "partials/search_panel.html", **ctx)
    return render(request, "search.html", active="search", **ctx)


@router.post("/verify", response_class=HTMLResponse)
async def verify(request: Request) -> HTMLResponse:
    require_htmx(request)
    f = await form_body(request)
    ds, ref, quote_text = f.get("ds", "").strip(), f.get("ref", "").strip(), f.get("quote", "").strip()
    if not (ds and ref and quote_text):
        return render(request, "partials/verify_result.html", chk=None, message=t("Paste a quote to verify."))

    def run() -> Any:
        with W.open_db() as conn:
            return verify_quote(conn, ds, ref, quote_text)

    try:
        chk = await run_in_threadpool(run)
    except Exception as e:  # noqa: BLE001
        return render(
            request,
            "partials/verify_result.html",
            chk=None,
            message=t("Verification failed: {error}", error=type(e).__name__),
        )
    return render(request, "partials/verify_result.html", chk=chk, ds=ds, message="")


# ====================================================================== sync
def _job_card(request: Request, job_id: str, poll: bool = False, page: str = "sync") -> HTMLResponse:
    job = W.get_job(job_id)
    return render(request, "partials/job_card.html", job=job, job_id=job_id, poll=poll, page=page)


def _alert(request: Request, message: str) -> HTMLResponse:
    return render(request, "partials/alert.html", tone="danger", icon_name="alert-circle", message=message)


@router.get("/sync", response_class=HTMLResponse)
def sync_page(request: Request) -> HTMLResponse:
    jobs = W.list_jobs(50)
    cat = W.catalog_info(request.app)
    return render(
        request,
        "sync.html",
        active="sync",
        jobs=jobs,
        live=next((j for j in jobs if j.active), None),
        jobs_ok=W.jobs_available(),
        reports=R.list_reports(C.REPORT_DIR),
        cat=cat,
        pdf_dir=str(C.DEFAULT_PDF_DIR),
    )


@router.get("/sync/jobs", response_class=HTMLResponse)
def sync_jobs(request: Request) -> HTMLResponse:
    return render(request, "partials/job_history.html", jobs=W.list_jobs(50), jobs_ok=W.jobs_available())


@router.get("/sync/jobs/{job_id}", response_class=HTMLResponse)
def sync_job(request: Request, job_id: str) -> HTMLResponse:
    page = "citations" if request.query_params.get("page") == "citations" else "sync"
    return _job_card(request, job_id, poll=request.query_params.get("poll") == "1", page=page)


@router.post("/sync/jobs/{job_id}/cancel", response_class=HTMLResponse)
def sync_job_cancel(request: Request, job_id: str) -> HTMLResponse:
    require_htmx(request)
    err = W.cancel_job(job_id)
    if err:  # shown next to the button; the card itself (and its polling) stays as it is
        resp = _alert(request, err)
        resp.headers["HX-Retarget"], resp.headers["HX-Reswap"] = "#cancel-msg", "innerHTML"
        return resp
    page = "citations" if request.query_params.get("page") == "citations" else "sync"
    return _job_card(request, job_id, poll=True, page=page)  # poll=True: the card keeps polling until the job ends


@router.post("/sync/catalog-refresh", response_class=HTMLResponse)
def sync_catalog_refresh(request: Request) -> HTMLResponse:
    require_htmx(request)
    job_id, err = W.submit_job("catalog_refresh", {})
    return _alert(request, err) if err or not job_id else _job_card(request, job_id, poll=True, page="sync")


@router.get("/sync/reports/{name}", response_class=HTMLResponse)
def sync_report(request: Request, name: str) -> HTMLResponse:
    rep = R.read_report(C.REPORT_DIR, name)
    if rep is None:
        return not_found(
            request, t("Report not found"), t("The report “{name}” does not exist in the reports folder.", name=name)
        )
    return render(request, "report.html", active="sync", rep=rep)


# ====================================================================== citations
def _filter_citations(request: Request) -> dict[str, Any]:
    qp = request.query_params
    status = qp.get("status", "")
    q = (qp.get("q") or "").strip().lower()
    offset = _int(qp.get("offset"), 0, 0, 10**7)
    data = W.citations_latest(request.app)
    rows = data.rows
    counts = {slug: sum(1 for r in rows if r.status == slug) for slug, _ in W.CITATION_SLUGS}
    if status in counts:
        rows = [r for r in rows if r.status == status]
    if q:
        rows = [r for r in rows if q in f"{r.file}:{r.line} {r.citation} {r.suggestion}".lower()]
    order = {"unknown": 0, "not_indexed": 1, "ok": 2}
    rows = sorted(rows, key=lambda r: (order.get(r.status, 3), r.file.lower(), r.line))
    page = rows[offset : offset + CITATIONS_PAGE]
    params = {"status": status, "q": qp.get("q", "")}
    return {
        "data": data,
        "rows": page,
        "n_filtered": len(rows),
        "n_all": len(data.rows),
        "counts": counts,
        "status": status,
        "q": qp.get("q", ""),
        "offset": offset,
        "page_size": CITATIONS_PAGE,
        "prev": W.url_with("/citations", params, offset=max(0, offset - CITATIONS_PAGE) or "") if offset else None,
        "next": W.url_with("/citations", params, offset=offset + CITATIONS_PAGE)
        if offset + CITATIONS_PAGE < len(rows)
        else None,
        "chips": [("", t("All"), len(data.rows)), *[(s, t(label), counts[s]) for s, label in W.CITATION_SLUGS]],
    }


@router.get("/citations", response_class=HTMLResponse)
def citations_page(request: Request) -> HTMLResponse:
    ctx = _filter_citations(request)
    jobs = W.list_jobs(20)
    live = next((j for j in jobs if j.kind == "check_citations" and j.active), None)
    if hx_target(request) == "citations-results":
        return render(request, "partials/citations_results.html", **ctx)
    return render(request, "citations.html", active="citations", live=live, jobs_ok=W.jobs_available(), **ctx)


@router.get("/citations/results", response_class=HTMLResponse)
def citations_results(request: Request) -> HTMLResponse:
    return render(request, "partials/citations_results.html", **_filter_citations(request))


@router.post("/citations/run", response_class=HTMLResponse)
async def citations_run(request: Request) -> HTMLResponse:
    require_htmx(request)
    f = await form_body(request)
    repo = f.get("repo", "").strip()
    job_id, err = await run_in_threadpool(W.submit_job, "check_citations", {"repo": repo} if repo else {})
    return _alert(request, err) if err or not job_id else _job_card(request, job_id, poll=True, page="citations")


# ====================================================================== MCP connection
SCOPE_LABELS = {
    "mcp:read": N_("MCP: AI agents"),
    "api:read": N_("REST API: read"),
    "api:write": N_("REST API: write"),
}
SCOPE_HINTS = {
    "mcp:read": N_("search and read through the MCP tools (read only)"),
    "api:read": N_("search, documents and reports through the REST API"),
    "api:write": N_("also start a sync, refresh the catalog, cancel jobs"),
}


def _keys_ctx(**extra: Any) -> dict[str, Any]:
    """What the key list on /connect needs; the keys module comes with the server package."""
    from vedun.server import keys
    from vedun.server import settings as S

    listed = keys.list_keys()
    return {
        "api_keys": listed,
        "scope_order": keys.SCOPES,
        "scope_labels": SCOPE_LABELS,
        "scope_hints": SCOPE_HINTS,
        "master_set": bool(S.secret("API_KEY")),
        "mcp_open": bool(S.secret("API_KEY")) or any(k.active and "mcp:read" in k.scopes for k in listed),
        **extra,
    }


def _no_store(resp: HTMLResponse) -> HTMLResponse:
    """A response that carries a key value must not be cached by the browser or a proxy."""
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request) -> HTMLResponse:
    return render(request, "connect.html", active="connect", **mcp_setup.context(request), **_keys_ctx())


@router.post("/connect/key", response_class=HTMLResponse)
def connect_show_master_key(request: Request) -> HTMLResponse:
    """The master key, on request only (never in the page's HTML), hidden again by the page after 60 s."""
    require_htmx(request)
    from vedun.server import audit
    from vedun.server import settings as S

    value = S.secret("API_KEY")
    if not value:
        raise HTTPException(404, t("The master key is not set on the server."))
    audit.log_event("key_shown", outcome="master key shown on the dashboard")
    return _no_store(render(request, "partials/master_key.html", shown=True, value=value, master_set=True))


@router.get("/connect/key/hidden", response_class=HTMLResponse)
def connect_hide_master_key(request: Request) -> HTMLResponse:
    require_htmx(request)
    from vedun.server import settings as S

    return render(request, "partials/master_key.html", shown=False, master_set=bool(S.secret("API_KEY")))


@router.post("/connect/keys", response_class=HTMLResponse)
async def connect_create_key(request: Request) -> HTMLResponse:
    require_htmx(request)
    from vedun.server import audit, keys

    raw = (await request.body()).decode("utf-8", errors="replace")
    form = parse_qs(raw, keep_blank_values=True)
    name = (form.get("name") or [""])[-1]
    expiry = (form.get("expiry") or ["90"])[-1]
    days = None if expiry == "never" else int(expiry) if expiry.isdigit() else -1
    try:
        rec, token = keys.create(name, form.get("scopes") or [], days)
    except ValueError:
        err = t("Give the key a name and at least one permission.")
        return render(request, "partials/api_keys.html", **_keys_ctx(key_error=err))
    audit.log_event("key_created", outcome=f"key {rec.name}", detail=" ".join(sorted(rec.scopes)))
    resp = render(request, "partials/api_keys.html", **_keys_ctx(new_token=token, new_name=rec.name))
    return _no_store(resp)


@router.post("/connect/keys/{key_id}/revoke", response_class=HTMLResponse)
def connect_revoke_key(request: Request, key_id: int) -> HTMLResponse:
    require_htmx(request)
    from vedun.server import audit, keys

    if keys.revoke(key_id):
        audit.log_event("key_revoked", outcome=f"key id {key_id}")
    return render(request, "partials/api_keys.html", **_keys_ctx())


# ====================================================================== media
@router.get("/media/figures/{ds}/{figure_id:path}")
def media_figure(ds: str, figure_id: str) -> FileResponse:
    with W.open_db() as conn:
        r = kb.figure(conn, ds, figure_id.removesuffix(".png"))
    if not r.ok or not r.path or not Path(r.path).is_file():
        raise HTTPException(404, t("No image for this figure."))
    return FileResponse(r.path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/media/pages/{ds}/{page}.png")
def media_page(ds: str, page: int) -> FileResponse:
    with W.open_db() as conn:
        r = kb.page_image(conn, ds, page)
    if not r.ok or not r.path or not Path(r.path).is_file():
        raise HTTPException(404, r.message or t("No image for this page."))
    return FileResponse(r.path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
