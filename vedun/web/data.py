"""Data access and formatting helpers for the web UI.

Everything that touches an *optional* integration (jobs module, catalog, citations, stats) is
defensive: a missing module or endpoint yields an empty value, never an exception, so every page
renders an empty state instead of a 500.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from vedun import config as C
from vedun import db as D
from vedun.embed import get_embedder
from vedun.rerank import get_reranker

from .i18n import N_, current, t

# ---------------------------------------------------------------------------- hooks (tests)


@dataclass
class Hooks:
    """Swap points so tests never touch the network."""

    embedder_factory: Callable[..., Any] = get_embedder
    reranker_factory: Callable[..., Any] = get_reranker
    #: ``(app, path) -> parsed JSON or None``; ``None`` = use the internal ASGI client
    api_get: Callable[[Any, str], Any] | None = None


hooks = Hooks()


# ---------------------------------------------------------------------------- database
@contextmanager
def open_db() -> Iterator[sqlite3.Connection]:
    """A per-request connection (sqlite objects are bound to the thread that made them)."""
    conn = D.connect()
    try:
        yield conn
    finally:
        conn.close()


def kind_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``ds -> {clause, table, figure, appendix}`` (primary units only, duplicates ignored)."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT doc_id, kind, COUNT(*) AS n FROM units WHERE dup_index=0 GROUP BY doc_id, kind"):
        out.setdefault(r["doc_id"], {})[r["kind"]] = r["n"]
    return out


def figure_rows(conn: sqlite3.Connection, ds: str) -> list[dict[str, Any]]:
    """Light list of the figures of a sheet for the gallery."""
    import json

    rows = []
    for r in conn.execute(
        "SELECT unit_id, title, page_start, extra FROM units WHERE doc_id=? AND kind='figure' AND dup_index=0 ORDER BY seq",
        (ds,),
    ):
        extra = json.loads(r["extra"] or "{}")
        rows.append(
            {
                "unit_id": r["unit_id"],
                "title": r["title"] or "",
                "caption": extra.get("caption") or r["title"] or "",
                "page": r["page_start"],
                "has_file": bool(extra.get("file")),
            }
        )
    return rows


# ---------------------------------------------------------------------------- labels
KIND_LABEL = {"clause": N_("Clause"), "table": N_("Table"), "figure": N_("Figure"), "appendix": N_("Appendix")}
KIND_ICON = {"clause": "hash", "table": "table", "figure": "image", "appendix": "file-text"}
KIND_OPTIONS = [
    ("", N_("All kinds")),
    ("clause", N_("Clauses")),
    ("table", N_("Tables")),
    ("figure", N_("Figures")),
    ("appendix", N_("Appendices")),
]

#: catalog status -> (label, icon, tone)
STATUS_META: dict[str, tuple[str, str, str]] = {
    "current": (N_("Current"), "check-circle", "ok"),
    "outdated": (N_("Needs refresh"), "refresh", "warn"),
    "missing_locally": (N_("Not local"), "download", "info"),
    "withdrawn": (N_("Withdrawn"), "ban", "muted"),
    "unknown": (N_("No data"), "help", "muted"),
}
STATUS_ORDER = ["current", "outdated", "missing_locally", "withdrawn"]
_STATUS_ALIASES = {
    "up_to_date": "current",
    "latest": "current",
    "ok": "current",
    "stale": "outdated",
    "update_available": "outdated",
    "missing": "missing_locally",
    "not_local": "missing_locally",
    "retired": "withdrawn",
}

JOB_KIND_LABEL = {
    "sync": N_("Document sync"),
    "check_citations": N_("Citation check"),
    "catalog_refresh": N_("Catalog refresh"),
}
JOB_STATUS_META: dict[str, tuple[str, str, str]] = {
    "queued": (N_("Queued"), "clock", "muted"),
    "running": (N_("Running"), "loader", "info"),
    "done": (N_("Done"), "check-circle", "ok"),
    "failed": (N_("Failed"), "alert-circle", "danger"),
    "cancelled": (N_("Cancelled"), "ban", "warn"),
}
CITATION_STATUS: dict[str, tuple[str, str, str, str]] = {
    # backend status -> (slug, label, icon, tone)
    "OK": ("ok", "OK", "check-circle", "ok"),
    "UNKNOWN UNIT": ("unknown", N_("UNKNOWN CLAUSE"), "alert-circle", "danger"),
    "DOCUMENT NOT INDEXED": ("not_indexed", N_("DOCUMENT NOT INDEXED"), "alert-triangle", "warn"),
}
CITATION_SLUGS = [("ok", N_("OK")), ("unknown", N_("Unknown clause")), ("not_indexed", N_("Document not indexed"))]
RERANKERS = [("none", N_("None (faster, no cost)")), ("r3", "Jev (TypeSafe)"), ("r1", "Cohere Rerank")]
RERANK_NAME = {"r1": "Cohere", "r2": "Qwen3", "r3": "Jev"}


def norm_status(v: Any) -> str:
    """Normalise a catalog status string to one of :data:`STATUS_META`."""
    if not isinstance(v, str) or not v.strip():
        return "unknown"
    s = re.sub(r"[\s\-]+", "_", v.strip().lower())
    s = _STATUS_ALIASES.get(s, s)
    return s if s in STATUS_META else "unknown"


# ---------------------------------------------------------------------------- formatting
def decimal(text: str) -> str:
    """Decimal comma in Polish, decimal point in English (``text`` is formatted with a point)."""
    return text.replace(".", ",") if current() == "pl" else text


def fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}".replace(",", "\u00a0" if current() == "pl" else ",")
    except (TypeError, ValueError):
        return "—"


def fmt_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else decimal(f"{n:.1f} {unit}")
        n /= 1024
    return "—"


def fmt_usd(v: Any) -> str:
    try:
        return decimal(f"{float(v):.2f}") + "\u00a0USD"
    except (TypeError, ValueError):
        return "—"


def to_dt(v: Any) -> datetime | None:
    """datetime | epoch seconds | ISO string -> local naive datetime (None when unparseable)."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, datetime):
            d = v
        elif isinstance(v, int | float):
            d = datetime.fromtimestamp(float(v), tz=UTC)
        else:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def fmt_dt(v: Any, seconds: bool = False) -> str:
    d = to_dt(v)
    if d is None:
        return "—"
    if current() == "pl":
        return d.strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")
    return d.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def fmt_duration(a: Any, b: Any = None) -> str:
    start, end = to_dt(a), to_dt(b) or datetime.now()
    if start is None:
        return "—"
    s = max(0, int((end - start).total_seconds()))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def url_with(path: str, params: dict[str, Any], **changes: Any) -> str:
    """``path?params`` with ``changes`` applied; empty / None values are dropped."""
    merged = {**params, **changes}
    clean = {k: v for k, v in merged.items() if v not in (None, "")}
    return f"{path}?{urlencode(clean)}" if clean else path


def dotted_key(ds: str) -> tuple[Any, ...]:
    """Natural sort key for document ids (``2-81`` < ``3-26`` < ``10-1``)."""
    m = re.match(r"^(\d+)-(\d+)(.*)$", ds)
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else (10**6, 0, ds)


# ---------------------------------------------------------------------------- internal API reads
def api_get(app: Any, path: str, timeout: float = 4.0) -> Any:
    """GET ``path`` from the mounted app in-process; parsed JSON or ``None`` (any failure)."""
    if hooks.api_get is not None:
        try:
            return hooks.api_get(app, path)
        except Exception:  # noqa: BLE001 - optional integration
            return None
    try:
        import httpx
    except ImportError:
        return None

    async def _go() -> Any:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://vedun.internal", timeout=timeout) as c:
            r = await c.get(path)
            return r.json() if r.status_code == 200 else None

    try:
        try:
            import anyio.from_thread

            return anyio.from_thread.run(_go)
        except RuntimeError:  # not inside a worker thread
            return asyncio.run(_go())
    except Exception:  # noqa: BLE001 - optional integration
        return None


def _items(data: Any, *keys: str) -> list[dict[str, Any]] | None:
    """The list of dicts inside ``data`` (a list or ``{key: [...]}``), else None."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return None


def _dig(d: Any, *paths: str) -> Any:
    """First non-None value among dotted ``paths`` in nested dicts."""
    for p in paths:
        cur = d
        for part in p.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur is not None:
            return cur
    return None


@dataclass
class CatalogInfo:
    """What the source catalog says about our documents (``available`` False = no catalog data yet)."""

    available: bool = False
    by_ds: dict[str, dict[str, Any]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def catalog_info(app: Any) -> CatalogInfo:
    items = _items(api_get(app, "/api/collection"), "documents", "items", "results")
    if items is None:
        return CatalogInfo()
    info = CatalogInfo(available=True)
    for it in items:
        ds = str(it.get("doc_id") or "").strip()
        if not ds:
            continue
        cat = it.get("catalog") if isinstance(it.get("catalog"), dict) else {}
        status = norm_status(it.get("catalog_status") or cat.get("status") or it.get("status"))
        info.by_ds[ds] = {
            "status": status,
            "title": it.get("title") or cat.get("title"),
            "latest": it.get("latest_edition_label")
            or it.get("latest_edition")
            or cat.get("edition_label")
            or cat.get("edition"),
            "local": it.get("local", it.get("indexed")),
        }
        info.counts[status] = info.counts.get(status, 0) + 1
    return info


def stats_info(app: Any) -> dict[str, Any]:
    """Optional extras from ``/api/stats`` (cost, last sync, catalog counts)."""
    d = api_get(app, "/api/stats")
    if not isinstance(d, dict):
        return {}
    return {
        "cost_usd": _dig(d, "cost_usd", "cost.total_usd", "cost.usd", "api_cost_usd", "costs.total_usd"),
        "last_sync": _dig(d, "last_sync", "last_sync_at", "sync.last", "sync.finished_at"),
        "catalog": _dig(d, "catalog", "catalog_counts"),
    }


# ---------------------------------------------------------------------------- jobs
@dataclass
class JobView:
    """Template-friendly job (works with a dataclass, pydantic model or dict)."""

    id: str
    kind: str
    status: str
    progress: float | None
    message: str
    log_tail: str
    params: dict[str, Any]
    created_at: Any
    started_at: Any
    finished_at: Any
    result: Any
    error: str | None
    cancel_requested: bool = False  # a running job was asked to stop and is finishing the current step

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def can_cancel(self) -> bool:
        """The Cancel button is offered: queued (any kind) or running with a safe point, not yet asked to stop."""
        if self.status == "queued":
            return True
        m = jobs_module()
        return (
            self.status == "running"
            and not self.cancel_requested
            and self.kind in getattr(m, "CANCELLABLE_WHILE_RUNNING", ())
        )

    @property
    def kind_label(self) -> str:
        return t(JOB_KIND_LABEL.get(self.kind, self.kind))

    @property
    def report_name(self) -> str | None:
        """A ``sync_*.md`` report mentioned in the result, if any."""

        def walk(v: Any) -> str | None:
            if isinstance(v, str):
                name = re.split(r"[\\/]", v)[-1]
                return name if re.fullmatch(r"sync_[\w\-]+\.md", name) else None
            if isinstance(v, dict):
                for x in v.values():
                    if (r := walk(x)) is not None:
                        return r
            if isinstance(v, list | tuple):
                for x in v:
                    if (r := walk(x)) is not None:
                        return r
            return None

        return walk(self.result)


def _get(o: Any, name: str, default: Any = None) -> Any:
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


_SYNC_SUMMARY = re.compile(
    r"^(Dry run: |Cancelled: )?added (\d+), replaced (\d+), removed (\d+), (unchanged|skipped) (\d+)(.*)$", re.S
)
_JOB_MESSAGE_PL = [
    (re.compile(r"^Done$"), "Zakończono"),
    (re.compile(r"^Interrupted by a service restart$"), "Przerwane przez restart usługi"),
    (re.compile(r"^Error: "), "Błąd: "),
    (re.compile(r"^Catalog fetched: (\d+) documents$"), r"Pobrano katalog: \1 dokumentów"),
    (re.compile(r"^Catalog: (\d+) documents"), r"Katalog: \1 dokumentów"),
    (
        re.compile(r"^Cancelled after (\d+)/(\d+) files; results were not saved$"),
        r"Anulowano po \1/\2 plikach; wyniki nie zostały zapisane",
    ),  # noqa: E501
    (re.compile(r"^Checked (\d+)/(\d+) files$"), r"Sprawdzono \1/\2 plików"),
    (re.compile(r"^(\d+) citations in (\d+) files:"), r"\1 cytowań w \2 plikach:"),
    (re.compile(r"daily API budget used up"), "dzienny budżet API wyczerpany"),
    (re.compile(r": sync needs embeddings"), ": synchronizacja wymaga embeddingów"),
]


def _sync_summary_pl(m: re.Match[str]) -> str:
    lead = {"Dry run: ": "Próba (dry run): ", "Cancelled: ": "Anulowano: "}.get(m[1] or "", "")
    last = "bez zmian" if m[5] == "unchanged" else "pominięto"
    rest = re.sub(r", failed (\d+):", r", nieudane \1:", m[7])
    return f"{lead}dodano {m[2]}, zastąpiono {m[3]}, usunięto {m[4]}, {last} {m[6]}{rest}"


def localize_job_message(s: str) -> str:
    """Job messages are stored in English (the API and MCP read them); the Polish UI translates their fixed phrases."""
    if current() != "pl" or not s:
        return s
    m = _SYNC_SUMMARY.match(s)
    if m:
        return _sync_summary_pl(m)
    for rx, repl in _JOB_MESSAGE_PL:
        s = rx.sub(repl, s)
    return s


def job_view(o: Any) -> JobView:
    status = str(_get(o, "status", "queued") or "queued").lower()
    prog = _get(o, "progress")
    try:
        prog_f: float | None = float(prog) if prog is not None else None
    except (TypeError, ValueError):
        prog_f = None
    if prog_f is not None and prog_f <= 1.0:
        prog_f *= 100.0
    if status == "done":
        prog_f = 100.0
    tail = _get(o, "log_tail", "")
    if isinstance(tail, list | tuple):
        tail = "\n".join(str(x) for x in tail)
    params = _get(o, "params", {})
    return JobView(
        id=str(_get(o, "id", "")),
        kind=str(_get(o, "kind", "")),
        status=status,
        progress=None if prog_f is None else max(0.0, min(100.0, prog_f)),
        message=localize_job_message(str(_get(o, "message", "") or "")),
        log_tail=str(tail or ""),
        params=params if isinstance(params, dict) else {},
        created_at=_get(o, "created_at"),
        started_at=_get(o, "started_at"),
        finished_at=_get(o, "finished_at"),
        result=_get(o, "result"),
        error=_get(o, "error") or None,
        cancel_requested=bool(_get(o, "cancel_requested", False)),
    )


def jobs_module() -> Any | None:
    """``vedun.server.jobs`` or None when the API agent's module is not importable."""
    try:
        import importlib

        return importlib.import_module("vedun.server.jobs")
    except Exception:  # noqa: BLE001 - optional integration
        return None


def jobs_available() -> bool:
    return jobs_module() is not None


def list_jobs(limit: int = 50) -> list[JobView]:
    m = jobs_module()
    if m is None:
        return []
    try:
        return [job_view(j) for j in m.list_jobs(limit)]
    except Exception:  # noqa: BLE001
        return []


def get_job(job_id: str) -> JobView | None:
    m = jobs_module()
    if m is None:
        return None
    try:
        return job_view(m.get_job(job_id))
    except Exception:  # noqa: BLE001 - unknown id or storage trouble
        return None


def submit_job(kind: str, params: dict[str, Any]) -> tuple[str | None, str]:
    """``(job_id, error)``; exactly one of them is empty."""
    m = jobs_module()
    if m is None:
        return None, t("The jobs module is not available in this instance.")
    try:
        return str(m.submit_job(kind, params)), ""
    except Exception as e:  # noqa: BLE001
        return None, t("Could not start the job: {error}", error=f"{type(e).__name__}: {str(e)[:200]}")


def cancel_job(job_id: str) -> str:
    """Ask the jobs module to cancel; returns an error text for the page, or ``""`` on success."""
    m = jobs_module()
    if m is None or not hasattr(m, "cancel_job"):
        return t("The jobs module is not available in this instance.")
    try:
        m.cancel_job(job_id)
    except KeyError:
        return t("No such job.")
    except Exception as e:  # noqa: BLE001 - NotCancellable has a machine-readable reason, the rest is reported as is
        reason = getattr(e, "reason", "")
        if reason == "finished":
            return t("This job has already finished, there is nothing to cancel.")
        if reason == "no_safe_point":
            return t("This job cannot be interrupted midway, it will finish by itself shortly.")
        return t("Cannot cancel the job: {error}", error=type(e).__name__)
    return ""


# ---------------------------------------------------------------------------- examples
@dataclass(frozen=True)
class SearchExamples:
    """Examples for placeholders and empty states, taken from the indexed collection (always real, always clickable)."""

    ds: str  # the first document (natural order) that has numbered clauses
    refs: tuple[tuple[str, str], ...]  # (kind, unit_id): its first clause and, if it has one, its first table
    phrases: tuple[str, ...]  # short clause headings offered as questions and phrases

    def locator(self, kind: str, unit_id: str) -> str:
        """``Section 3.5`` / ``pkt 3.5`` in the current language, from the profile's locators."""
        tpl = C.LOCATORS.get(current(), C.LOCATORS["en"]).get(kind, "{id}")
        return tpl.format(id=unit_id)

    @property
    def clause(self) -> str:
        """``<doc label> <locator of the first clause>``, e.g. ``NIST SP 800-30 Section 2.1``."""
        kind, uid = self.refs[0]
        return f"{C.doc_label(self.ds)} {self.locator(kind, uid)}"


_HEADING = re.compile(r"^[^\W\d_][^\d]{2,60}$")


def search_examples() -> SearchExamples | None:
    """None for an empty knowledge base; the queries touch one document plus a bounded scan of headings."""
    try:
        with open_db() as conn:
            docs = [r[0] for r in conn.execute("SELECT DISTINCT doc_id FROM units WHERE kind='clause' AND dup_index=0")]
            if not docs:
                return None
            ds = min(docs, key=dotted_key)
            refs: list[tuple[str, str]] = []
            for kind in ("clause", "table"):
                r = conn.execute(
                    "SELECT unit_id FROM units WHERE doc_id=? AND kind=? AND dup_index=0 ORDER BY seq LIMIT 1",
                    (ds, kind),
                ).fetchone()
                if r:
                    refs.append((kind, r[0]))
            phrases: list[str] = []
            for (title,) in conn.execute(
                "SELECT title FROM units WHERE kind='clause' AND dup_index=0 AND title IS NOT NULL ORDER BY seq LIMIT 400"
            ):
                words = str(title).split()
                if 2 <= len(words) <= 6 and _HEADING.match(str(title)) and title not in phrases:
                    phrases.append(str(title))
                if len(phrases) == 2:
                    break
    except sqlite3.Error:
        return None
    return SearchExamples(ds, tuple(refs), tuple(phrases))


# ---------------------------------------------------------------------------- citations
@dataclass
class CitationRow:
    file: str
    line: int
    citation: str
    ds: str
    unit_id: str
    status: str  # slug: ok | unknown | not_indexed
    label: str
    icon: str
    tone: str
    suggestion: str
    has_note: bool


_SUGGESTION_PL = [
    (re.compile(r"renumbered to", re.I), "przenumerowano na"),
    (re.compile(r"nearest:", re.I), "najbliższe:"),
    (re.compile(r"note: cited edition (\S+), current (\S+)", re.I), r"uwaga: cytowana edycja \1, obecna \2"),
]


def localize_suggestion(s: str) -> str:
    """The backend suggestion is English; the Polish UI translates its three fixed phrases."""
    if current() != "pl":
        return s
    for rx, repl in _SUGGESTION_PL:
        s = rx.sub(repl, s)
    return s


def citation_row(d: dict[str, Any]) -> CitationRow:
    raw = str(d.get("status") or "").strip().upper()
    slug, label, icon, tone = CITATION_STATUS.get(raw, ("unknown", raw or N_("UNKNOWN"), "help", "muted"))
    sug = str(d.get("suggestion") or "")
    try:
        line = int(d.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    return CitationRow(
        file=str(d.get("file") or ""),
        line=line,
        citation=str(d.get("citation") or d.get("text") or ""),
        ds=str(d.get("doc_id") or ""),
        unit_id=str(d.get("unit_id") or ""),
        status=slug,
        label=t(label),
        icon=icon,
        tone=tone,
        suggestion=localize_suggestion(sug),
        has_note=slug == "ok" and bool(sug),
    )


@dataclass
class CitationsData:
    rows: list[CitationRow] = field(default_factory=list)
    available: bool = False
    generated_at: Any = None
    source: str = ""


def citations_latest(app: Any) -> CitationsData:
    """Latest check-citations results: the REST endpoint first, then the newest finished job."""
    data = api_get(app, "/api/citations/latest")
    items = _items(data, "results", "items", "citations", "rows")
    if items is not None:
        return CitationsData(
            [citation_row(x) for x in items],
            True,
            _dig(data, "generated_at", "finished_at", "created_at") if isinstance(data, dict) else None,
            "api",
        )
    for j in list_jobs(50):
        if j.kind == "check_citations" and j.status == "done":
            items = _items(j.result, "results", "items", "citations", "rows")
            if items is not None:
                return CitationsData([citation_row(x) for x in items], True, j.finished_at, "job")
    return CitationsData()
