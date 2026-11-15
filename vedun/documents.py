"""Whole-document access: metadata, host/container paths, Markdown (paginated, optionally one section), PDF location.

Evidence records answer "which clause says X"; agents sometimes need the whole sheet or a whole chapter. This module
returns the Markdown (verbatim from ``md/``, split on blank lines where possible) and tells the caller where the
original PDF and the Markdown file are, **as the host sees them** (``VEDUN_HOST_PDF_DIR``, ``VEDUN_HOST_ROOT`` /
``VEDUN_HOST_MD_DIR``) and as this process sees them. PDF bytes are never returned inline.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

from . import config as C
from .catalog import scan_pdfs
from .convert import edition_tag
from .evidence import make_citation
from .search import lookup, toc

MAX_PAGE_CHARS = 200_000


def _fwd(p: str | Path) -> str:
    return str(p).replace("\\", "/")


def host_path(local: Path | str, kind: Literal["pdf", "md"]) -> str:
    """The path of ``local`` as the Windows host sees it.

    ``VEDUN_HOST_PDF_DIR`` maps the PDF folder; ``VEDUN_HOST_MD_DIR`` (or ``VEDUN_HOST_ROOT``/md) maps the Markdown
    folder. Without the variables (running on the host) the local path already is the host path.
    """

    local = Path(local)
    if kind == "pdf":
        base_host = C.env("HOST_PDF_DIR")
        base_local = C.DEFAULT_PDF_DIR
    else:
        base_host = C.env("HOST_MD_DIR") or (_fwd(Path(C.env("HOST_ROOT")) / "md") if C.env("HOST_ROOT") else None)
        base_local = C.MD_DIR
    if base_host:
        try:
            return _fwd(base_host).rstrip("/") + "/" + local.relative_to(base_local).as_posix()
        except ValueError:
            return _fwd(local)
    return _fwd(local)


def paths_for(ds: str, source_pdf: str | None, edition: str | None) -> dict[str, Any]:
    """Host and container paths of a sheet's original PDF and Markdown (``None`` when the file does not exist)."""
    pdf = C.DEFAULT_PDF_DIR / source_pdf if source_pdf else None
    md = C.MD_DIR / f"{ds}_{edition_tag(edition)}.md" if edition else None
    out: dict[str, Any] = {}
    for key, p, kind in (("pdf", pdf, "pdf"), ("md", md, "md")):
        ok = p is not None and p.is_file()
        out[f"{key}_exists"] = ok
        out[f"{key}_path_container"] = _fwd(p) if ok else None
        out[f"{key}_path_host"] = host_path(p, kind) if ok else None  # type: ignore[arg-type]
    return out


def _check_ds(ds: str) -> None:
    if not C.doc_id_re().fullmatch(ds):
        raise ValueError(f"bad document id {ds!r}")


def _sheet(conn: sqlite3.Connection, ds: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE doc_id=?", (ds,)).fetchone()


def list_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every sheet that has an original or a Markdown: indexed ones and PDFs only on disk."""
    rows = {r["doc_id"]: r for r in conn.execute("SELECT * FROM documents ORDER BY doc_id")}
    local = scan_pdfs()
    out = []
    from .catalog import dotted_key

    for ds in sorted({*rows, *local}, key=dotted_key):
        r = rows.get(ds)
        pdf_name = r["source_pdf"] if r else local[ds][1]
        ed = r["edition"] if r else None
        out.append(
            {
                "doc_id": ds,
                "title": r["title"] if r else None,
                "indexed": r is not None,
                "edition": ed or (local[ds][0] if ds in local else None),
                "edition_label": r["edition_label"] if r else None,
                **paths_for(ds, pdf_name, ed),
            }
        )
    return out


def _meta(conn: sqlite3.Connection, ds: str) -> dict[str, Any] | None:
    r = _sheet(conn, ds)
    if r is None:
        local = scan_pdfs().get(ds)
        if local is None:
            return None
        return {
            "doc_id": ds,
            "indexed": False,
            "title": None,
            "edition": local[0],
            "edition_label": None,
            "citation_prefix": None,
            "units": {},
            "toc": [],
            "paths": paths_for(ds, local[1], None),
            "message": "PDF is on disk but the sheet is not indexed: no Markdown, no structure.",
        }
    kinds = {
        k["kind"]: k["n"]
        for k in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM units WHERE doc_id=? AND dup_index=0 GROUP BY kind", (ds,)
        )
    }
    t = toc(conn, ds, None, depth=0, limit=200)
    return {
        "doc_id": ds,
        "indexed": True,
        "title": r["title"],
        "edition": r["edition"],
        "edition_label": r["edition_label"],
        "citation_prefix": make_citation(ds, r["title"], r["edition_label"], r["edition"], "clause", "", "pl").rsplit(
            ", ", 1
        )[0],
        "units": kinds,
        "toc": [{"unit_id": i.unit_id, "kind": i.kind, "title": i.title, "n_children": i.n_children} for i in t.items],
        "pages": r["pages"],
        "paths": paths_for(ds, r["source_pdf"], r["edition"]),
    }


def _cut(text: str, offset: int, max_chars: int) -> tuple[str, int | None]:
    """``text[offset:end]`` where ``end`` backs up to a blank line (unit boundary) when one lies in the 2nd half."""
    end = offset + max_chars
    if end >= len(text):
        return text[offset:], None
    cut = text.rfind("\n\n", offset + max_chars // 2, end)
    if cut != -1:
        end = cut + 2
    return text[offset:end], end


def get_document(
    conn: sqlite3.Connection,
    ds: str,
    fmt: Literal["md", "pdf", "meta"] = "meta",
    section: str | None = None,
    offset: int = 0,
    max_chars: int = 60_000,
) -> dict[str, Any]:
    """Metadata + paths (``meta`` / ``pdf``), or a page of the Markdown (``md``).

    ``md``: ``section="2.3.4"`` restricts to that clause and everything under it (from the unit line ranges);
    ``offset`` / ``max_chars`` paginate; pages are contiguous, so concatenating them gives the full text.
    ``total_chars`` and ``next_offset`` (``None`` on the last page) drive the loop. ``pdf`` never returns bytes.
    Raises ``ValueError`` for a malformed ``doc_id``; ``found=False`` when nothing matches.
    """
    _check_ds(ds)
    meta = _meta(conn, ds)
    if meta is None:
        return {"found": False, "doc_id": ds, "message": f"{C.doc_label(ds)} is neither indexed nor on disk"}
    meta["found"] = True
    if fmt == "meta":
        return meta
    if fmt == "pdf":
        meta["url"] = f"/api/documents/{ds}.pdf"
        meta["note"] = "open pdf_path_host with a file reader; PDF bytes are never inlined"
        return meta
    if fmt != "md":
        raise ValueError("fmt must be 'md', 'pdf' or 'meta'")
    md_file = C.MD_DIR / f"{ds}_{edition_tag(meta['edition'])}.md"
    if not meta["indexed"] or not md_file.is_file():
        return {**meta, "found": False, "message": "no Markdown for this sheet (not indexed or file missing)"}
    text = md_file.read_text(encoding="utf-8")
    if section:
        it = lookup(conn, ds, section, include_children=True).items[0]
        if not it.found or it.evidence is None:
            return {**meta, "found": False, "section": section, "message": it.message, "nearest": it.nearest}
        units = [it.evidence, *it.children]
        starts = [u.md_line_start for u in units if u.md_line_start]
        ends = [u.md_line_end for u in units if u.md_line_end]
        lines = text.splitlines(keepends=True)
        text = "".join(lines[min(starts) - 1 : max(ends)])
        meta["section"] = it.evidence.unit_id
    offset = max(0, int(offset))
    max_chars = max(1000, min(int(max_chars), MAX_PAGE_CHARS))
    page, nxt = _cut(text, offset, max_chars)
    return {
        "found": True,
        "doc_id": ds,
        "format": "md",
        "title": meta["title"],
        "edition_label": meta["edition_label"],
        "citation_prefix": meta["citation_prefix"],
        "section": meta.get("section"),
        "paths": meta["paths"],
        "text": page,
        "offset": offset,
        "returned_chars": len(page),
        "total_chars": len(text),
        "next_offset": nxt,
    }


def pdf_file(ds: str, conn: sqlite3.Connection | None = None) -> Path | None:
    """The original PDF of ``ds`` (the indexed edition, else the newest on disk), guarded against path tricks."""
    _check_ds(ds)
    r = _sheet(conn, ds) if conn is not None else None
    name = r["source_pdf"] if r else (scan_pdfs().get(ds) or (None, None))[1]
    if not name or not C.pdf_name_re().fullmatch(name):
        return None
    p = (C.DEFAULT_PDF_DIR / name).resolve()
    return p if p.is_file() and p.is_relative_to(C.DEFAULT_PDF_DIR.resolve()) else None
