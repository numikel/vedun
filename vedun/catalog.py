"""Document catalog of a remote source: which editions exist, which of ours are outdated, download of new PDFs.

The source itself is a plugin named in the corpus profile (``source_plugin``): a Python file that defines
``SOURCE``, an object with ``name``, ``fetch() -> Catalog`` and ``download(entry, dest) -> int`` (see
:class:`Source`). Without a plugin the catalog functions raise :class:`NoSourceError`; the PDF folder, upload
and sync still work. This module holds everything that does not depend on the source:

* :func:`fetch_catalog` asks the source (one call), validates the entries and caches the snapshot with a timestamp
  in the sidecar state DB (:mod:`vedun.state`); :func:`cached_catalog` returns the last snapshot.
* :func:`catalog_status` compares the catalog with the PDF folder and the indexed sheets and labels every
  sheet ``current`` / ``outdated`` / ``missing_locally`` / ``withdrawn``.
* :func:`download_updates` fetches the PDFs of newer editions next to the old ones. It NEVER deletes anything:
  the caller (the ``sync`` job) removes the old PDF with :func:`retire_old_pdfs` only after the new edition
  was indexed successfully, so a failed conversion leaves the previous edition intact.
* :func:`fill_source_item_ids` writes the source's item id into ``documents.source_item_id`` (shown in Evidence).
* :func:`http_json` / :func:`http_download` are polite HTTP helpers for plugins: strictly sequential requests
  with a pause of :data:`PAUSE_S` seconds, an identifying User-Agent, a ``%PDF`` check and a size cap.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from . import config as C
from .state import connect_state

USER_AGENT = "vedun/0.1 (local knowledge base; sequential requests)"
PAUSE_S = 0.5
MAX_PDF_BYTES = 150_000_000

Status = Literal["current", "outdated", "missing_locally", "withdrawn"]
_EDITION = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


# ================================================================================ data
@dataclass
class CatalogEntry:
    """One document in the source's catalog."""

    doc_id: str
    title: str
    item_id: str  # the source's own id of the document (whatever ``download`` needs)
    published: str | None  # "October 2026" as shown by the source
    edition: str | None  # "2026-10" (same form as ``documents.edition``)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Catalog:
    """Snapshot of the catalog with the time it was fetched."""

    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    entries: dict[str, CatalogEntry] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "n_entries": len(self.entries),
            "warnings": self.warnings,
            "entries": [e.to_dict() for e in self.entries.values()],
        }


@dataclass
class DocumentStatus:
    """Catalog status of one document id (union of catalog, PDF folder and index)."""

    doc_id: str
    status: str  # current | outdated | missing_locally | withdrawn | unknown (no catalog fetched yet)
    title: str | None = None
    latest_edition: str | None = None
    latest_edition_label: str | None = None
    source_item_id: str | None = None
    local_pdf: str | None = None
    local_edition: str | None = None
    indexed: bool = False
    indexed_edition: str | None = None
    index_stale: bool = False  # indexed edition differs from the newest PDF in the folder

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Downloaded:
    """A PDF fetched by :func:`download_updates`; ``replaces`` are the old files still on disk."""

    doc_id: str
    file: str
    edition: str
    bytes: int
    replaces: list[str] = field(default_factory=list)


@dataclass
class DownloadReport:
    """Outcome of :func:`download_updates`. Nothing on disk was deleted."""

    downloaded: list[Downloaded] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ================================================================================ parsing (pure)
def edition_from_published(published: str | None) -> str | None:
    """``"October 2026"`` -> ``"2026-10"``; ``None`` when not a month + year."""
    if not published:
        return None
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", published.strip())
    if not m or m.group(1).capitalize() not in _MONTHS:
        return None
    return f"{m.group(2)}-{_MONTHS.index(m.group(1).capitalize()) + 1:02d}"


def validated(cat: Catalog) -> Catalog:
    """Drop entries a source must not hand over (trust boundary): the id and edition become a file name.

    Kept: ``doc_id`` matches the profile's ``doc_id_pattern`` and is the dict key, ``edition`` is ``YYYY-MM`` or None.
    Every dropped entry is named in ``warnings``.
    """
    id_re = C.doc_id_re()
    for key, e in list(cat.entries.items()):
        why = (
            "id does not match doc_id_pattern"
            if not isinstance(e.doc_id, str) or not id_re.match(e.doc_id)
            else "key differs from the entry id"
            if key != e.doc_id
            else "edition is not YYYY-MM"
            if e.edition is not None and not _EDITION.match(str(e.edition))
            else None
        )
        if why:
            del cat.entries[key]
            cat.warnings.append(f"entry {key!r} dropped: {why}")
    return cat


# ================================================================================ source plugin
class Source(Protocol):
    """What a ``source_plugin`` file exposes as ``SOURCE``."""

    name: str  # shown in job logs and in the dashboard next to catalog wording ("example.org")

    def fetch(self) -> Catalog:
        """The current catalog (one or more requests). Raise on any network or format error."""
        ...

    def download(self, entry: CatalogEntry, dest: Path) -> int:
        """Write the PDF of ``entry`` to ``dest`` (a ``.part`` file); return the byte count."""
        ...


class NoSourceError(RuntimeError):
    """The profile names no ``source_plugin``: there is no remote catalog to read or download from."""


_loaded: dict[Path, Source] = {}


def load_source() -> Source:
    """``SOURCE`` from the profile's ``source_plugin`` file (imported once per path)."""
    path = C.SOURCE_PLUGIN
    if path is None:
        raise NoSourceError(
            "no document source configured: set 'source_plugin' in the corpus profile "
            "(a Python file defining SOURCE with fetch() and download(entry, dest))"
        )
    if path not in _loaded:
        spec = importlib.util.spec_from_file_location(f"vedun_source_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise NoSourceError(f"source_plugin {path} cannot be imported")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod  # dataclasses and pickling in the plugin look their module up here
        spec.loader.exec_module(mod)
        src = getattr(mod, "SOURCE", None)
        if not (callable(getattr(src, "fetch", None)) and callable(getattr(src, "download", None))):
            raise NoSourceError(f"source_plugin {path} defines no SOURCE with fetch() and download()")
        _loaded[path] = src
    return _loaded[path]


def source_name() -> str | None:
    """The configured source's ``name`` for wording in the dashboard; None without a usable ``source_plugin``."""
    try:
        name = getattr(load_source(), "name", None)
    except Exception:  # noqa: BLE001 - a broken plugin must not break page rendering, the real error shows on use
        return None
    return str(name or "").strip() or None


def dotted_key(ds: str) -> tuple[int, int, str]:
    """Natural sort key: ``2-9`` < ``4-12`` < ``11-7`` < ``11-7A`` < ``12-1``."""
    m = re.match(r"^(\d+)-(\d+)(.*)$", ds)
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else (10**6, 0, ds)


# ================================================================================ network
_last_request = 0.0


def _pause(seconds: float = PAUSE_S) -> None:
    """Sequential-requests politeness: at least ``seconds`` between two requests."""
    global _last_request
    wait = _last_request + seconds - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def http_json(url: str, *, user_agent: str = USER_AGENT) -> Any:
    """GET ``url`` and decode JSON (sequential, with the politeness pause)."""
    _pause()
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_catalog(
    *,
    max_age_s: float | None = None,
    force: bool = False,
    source: Source | None = None,
    state_db: Path | str | None = None,
) -> Catalog:
    """The catalog of ``source`` (default: the profile's plugin), cached in the state DB with a timestamp.

    ``max_age_s``: reuse the cached snapshot if it is younger (unless ``force``). Default = always fetch.
    On a network error the previous snapshot is NOT returned silently: the exception propagates.
    """
    if not force and max_age_s is not None:
        cached = cached_catalog(state_db)
        if cached and (datetime.now(UTC) - datetime.fromisoformat(cached.fetched_at)).total_seconds() < max_age_s:
            return cached
    src = source or load_source()
    cat = validated(src.fetch())
    if not cat.entries:
        raise RuntimeError(f"{src.name} returned no documents (empty catalog) - cache left untouched")
    conn = connect_state(state_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO catalog_snapshot(id, fetched_at, payload) VALUES(1, ?, ?)",
            (cat.fetched_at, json.dumps([e.to_dict() for e in cat.entries.values()], ensure_ascii=False)),
        )
    finally:
        conn.close()
    return cat


def cached_catalog(state_db: Path | str | None = None) -> Catalog | None:
    """Last cached snapshot, or ``None`` when the catalog was never fetched."""
    conn = connect_state(state_db)
    try:
        r = conn.execute("SELECT fetched_at, payload FROM catalog_snapshot WHERE id=1").fetchone()
    finally:
        conn.close()
    if not r:
        return None
    cat = Catalog(fetched_at=r["fetched_at"])
    try:
        for d in json.loads(r["payload"]):
            cat.entries[d["doc_id"]] = CatalogEntry(**d)
    except (KeyError, TypeError):  # a snapshot written by an older version: treat as "never fetched"
        return None
    return cat


# ================================================================================ status
def scan_pdfs(pdf_dir: Path | None = None) -> dict[str, tuple[str, str]]:
    """``ds -> (edition "YYYY-MM", file name)`` of the NEWEST PDF per sheet in ``pdf_dir``."""
    out: dict[str, tuple[str, str]] = {}
    d = pdf_dir or C.DEFAULT_PDF_DIR
    if not d.is_dir():
        return out
    name_re = C.pdf_name_re()
    for f in sorted(d.iterdir()):
        m = name_re.match(f.name)
        if not m:
            continue
        ed = f"{m.group(2)[:4]}-{m.group(2)[4:]}"
        if m.group(1) not in out or ed > out[m.group(1)][0]:
            out[m.group(1)] = (ed, f.name)
    return out


def catalog_status(
    conn: sqlite3.Connection | None = None,
    catalog: Catalog | None = None,
    pdf_dir: Path | None = None,
) -> list[DocumentStatus]:
    """Status of every sheet known to the catalog, the PDF folder or the index (natural order).

    ``current``: newest local PDF is as new as the catalog; ``outdated``: the catalog has a newer edition;
    ``missing_locally``: in the catalog, no PDF in the folder; ``withdrawn``: on disk / indexed but no longer
    in the catalog. Without a catalog snapshot every sheet is ``unknown``.
    """
    local = scan_pdfs(pdf_dir)
    indexed: dict[str, dict[str, Any]] = {}
    if conn is not None:
        indexed = {
            r["doc_id"]: dict(r) for r in conn.execute("SELECT doc_id, edition, title, source_item_id FROM documents")
        }
    entries = catalog.entries if catalog else {}
    out = []
    for ds in sorted({*entries, *local, *indexed}, key=dotted_key):
        e = entries.get(ds)
        loc = local.get(ds)
        idx = indexed.get(ds)
        if catalog is None:
            status = "unknown"
        elif e is None:
            status = "withdrawn"
        elif loc is None:
            status = "missing_locally"
        elif e.edition and loc[0] < e.edition:
            status = "outdated"
        else:
            status = "current"
        out.append(
            DocumentStatus(
                doc_id=ds,
                status=status,
                title=(e.title if e else None) or (idx or {}).get("title"),
                latest_edition=e.edition if e else None,
                latest_edition_label=e.published if e else None,
                source_item_id=(e.item_id if e else None) or (idx or {}).get("source_item_id"),
                local_pdf=loc[1] if loc else None,
                local_edition=loc[0] if loc else None,
                indexed=idx is not None,
                indexed_edition=idx["edition"] if idx else None,
                index_stale=bool(idx and loc and idx["edition"] != loc[0]),
            )
        )
    return out


def counts_by_status(rows: list[DocumentStatus]) -> dict[str, int]:
    """``{status: n}`` for the dashboard overview."""
    out: dict[str, int] = {}
    for r in rows:
        out[r.status] = out.get(r.status, 0) + 1
    return out


# ================================================================================ download
def http_download(url: str, dest: Path, *, user_agent: str = USER_AGENT) -> int:
    """Stream ``url`` to ``dest``; returns the byte count. Raises on a non-PDF or an oversized body."""
    _pause()
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/pdf"})
    n = 0
    with urllib.request.urlopen(req, timeout=180) as r, dest.open("wb") as fh:
        head = r.read(1024)
        if not head.startswith(b"%PDF"):
            raise ValueError(f"response is not a PDF (starts with {head[:20]!r})")
        fh.write(head)
        n = len(head)
        while chunk := r.read(1 << 20):
            n += len(chunk)
            if n > MAX_PDF_BYTES:
                raise ValueError(f"PDF larger than {MAX_PDF_BYTES // 1_000_000} MB")
            fh.write(chunk)
    return n


def download_pdf(entry: CatalogEntry, dest_dir: Path, source: Source | None = None) -> Downloaded:
    """Download one sheet to ``dest_dir/<ds>_<YYYYMM>.pdf`` (via ``.part``; validated ``%PDF`` header)."""
    if not entry.edition:
        raise ValueError(f"{C.doc_label(entry.doc_id)}: the catalog gives no publication month, cannot name the file")
    final = dest_dir / f"{entry.doc_id}_{entry.edition.replace('-', '')}.pdf"
    if not C.pdf_name_re().match(final.name):  # the name is built from source data: never a path elsewhere
        raise ValueError(f"refusing file name {final.name!r} built from the catalog entry")
    dest_dir.mkdir(parents=True, exist_ok=True)
    part = final.with_suffix(".pdf.part")
    try:
        n = (source or load_source()).download(entry, part)
        with part.open("rb") as fh:
            if fh.read(4) != b"%PDF":
                raise ValueError("downloaded file has no %PDF header")
        os.replace(part, final)
    finally:
        part.unlink(missing_ok=True)
    return Downloaded(entry.doc_id, final.name, entry.edition, n)


def download_updates(
    dest_dir: Path,
    *,
    catalog: Catalog,
    only: list[str] | None = None,
    include_missing: bool = False,
    source: Source | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> DownloadReport:
    """Fetch the PDFs of ``outdated`` sheets (and ``missing_locally`` ones when asked) into ``dest_dir``.

    Scope = ``only`` (sheet numbers) or, by default, every outdated sheet. The OLD PDF stays where it is -
    deleting it is the caller's job, after the new edition indexed OK (:func:`retire_old_pdfs`). Requests are
    sequential with a pause; one failed sheet does not stop the others (see ``errors``).
    """
    rep = DownloadReport()
    src = source or load_source()  # before the loop: a missing plugin is one error, not one per sheet
    for st in catalog_status(None, catalog, dest_dir):
        if only is not None and st.doc_id not in only:
            continue
        wanted = st.status == "outdated" or (include_missing and st.status == "missing_locally")
        if not wanted:
            if only is not None:
                rep.skipped.append(f"{st.doc_id}: {st.status}")
            continue
        entry = catalog.entries[st.doc_id]
        try:
            d = download_pdf(entry, dest_dir, src)
        except Exception as e:  # noqa: BLE001 - one sheet must not stop the batch
            rep.errors[st.doc_id] = f"{type(e).__name__}: {str(e)[:200]}"
            log(f"[{st.doc_id}] download failed: {rep.errors[st.doc_id]}")
            continue
        d.replaces = sorted(p.name for p in dest_dir.glob(f"{st.doc_id}_??????.pdf") if p.name != d.file)
        rep.downloaded.append(d)
        log(f"[{st.doc_id}] downloaded {d.file} ({d.bytes / 1e6:.1f} MB), replaces {', '.join(d.replaces) or '-'}")
    return rep


def retire_old_pdfs(pdf_dir: Path, ds: str, keep_file: str) -> list[str]:
    """Delete every other edition of ``ds`` in ``pdf_dir`` (call only after ``keep_file`` indexed OK)."""
    removed = []
    for f in sorted(pdf_dir.glob(f"{ds}_??????.pdf")):
        if f.name != keep_file:
            f.unlink()
            removed.append(f.name)
    return removed


# ================================================================================ Evidence link
def fill_source_item_ids(conn: sqlite3.Connection, catalog: Catalog) -> int:
    """Write the source's item id into ``documents.source_item_id`` for indexed documents; returns the rows changed."""
    n = 0
    for r in conn.execute("SELECT doc_id, source_item_id FROM documents").fetchall():
        e = catalog.entries.get(r["doc_id"])
        if e and r["source_item_id"] != e.item_id:
            conn.execute("UPDATE documents SET source_item_id=? WHERE doc_id=?", (e.item_id, r["doc_id"]))
            n += 1
    return n
