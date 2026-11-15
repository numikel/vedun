"""``verify_quote``: final check before a statement goes into a mail, report or opinion.

A quote is verified (a) inside the stated unit's text and (b) in the document's PDF text layer
(anywhere; page numbers are not part of the identity). If the quote sits in another unit, that
unit is reported. Comparison ignores whitespace, hyphenation, quote/dash typography and Docling's
``ft 2`` / ``ft2`` superscript spacing, but is otherwise exact (digits and letters must match).
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config as C
from .evidence import Evidence
from .search import _Ctx, parse_ref

_PDF_CACHE: dict[str, tuple[str, list[tuple[int, int]]]] = {}  # path -> (normalised text, [(start, page)])


def normalize_text(t: str) -> str:
    """Readable normal form: NFKC, straight quotes/dashes, no soft hyphens, hyphenation joined."""
    t = unicodedata.normalize("NFKC", t)
    t = t.replace("­", "").replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"')
    t = re.sub(r"[‐-―−]", "-", t)
    t = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", t)  # line-end hyphenation
    t = re.sub(r"\*\*|\|", " ", t)  # Markdown emphasis / table pipes
    return re.sub(r"\s+", " ", t).strip()


def match_key(t: str) -> str:
    """Comparison key: normalised text without whitespace and without letter-letter hyphens."""
    t = normalize_text(t)
    t = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", "", t)
    return re.sub(r"\s+", "", t)


@dataclass
class QuoteCheck:
    """Outcome of :func:`verify_quote` (JSON-serialisable)."""

    match: bool  # in the stated unit AND in the PDF text layer
    in_unit: bool
    in_pdf: bool | None  # None = PDF unavailable (see pdf_message)
    pdf_message: str = ""
    quote: str = ""
    requested_ref: str = ""
    found_in_units: list[str] = field(default_factory=list)  # other units that contain the quote verbatim
    pdf_pages: list[int] = field(default_factory=list)  # informational locator only
    closest: str | None = None
    closest_ratio: float = 0.0
    closest_unit_id: str | None = None
    evidence: Evidence | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _page_body(text: str, title: str, ds: str) -> str:
    """Page text without running headers/footers (a quote may span a page break)."""
    from .convert import is_boilerplate_line

    keep = []
    for ln in text.splitlines():
        t = ln.strip()
        if is_boilerplate_line(t) or t == ds or (title and t.lower() == title.lower()):
            continue
        keep.append(ln)
    return "\n".join(keep)


def _pdf_index(path: Path, title: str = "", ds: str = "") -> tuple[str, list[tuple[int, int]]]:
    key = str(path)
    if key not in _PDF_CACHE:
        from .pdftext import page_texts

        parts, starts, pos = [], [], 0
        for i, text in enumerate(page_texts(path), start=1):
            k = match_key(_page_body(text, title, ds))
            starts.append((pos, i))
            parts.append(k)
            pos += len(k)
        _PDF_CACHE[key] = ("".join(parts), starts)
    return _PDF_CACHE[key]


def _pages_of(text: str, starts: list[tuple[int, int]], qk: str) -> list[int]:
    pages, i = [], text.find(qk)
    while i != -1 and len(pages) < 20:
        pg = 1
        for s, p in starts:
            if s <= i:
                pg = p
            else:
                break
        if pg not in pages:
            pages.append(pg)
        i = text.find(qk, i + 1)
    return pages


def _closest(text: str, quote: str) -> tuple[str, float]:
    """Best word-window of ``text`` similar to ``quote`` (difflib ratio on normalised strings)."""
    q = normalize_text(quote)
    words = normalize_text(text).split(" ")
    n = max(1, len(q.split(" ")))
    best, best_r = "", 0.0
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(q)
    for i in range(0, max(1, len(words) - n + 1)):
        for w in (n, n + 2) if n > 3 else (n,):
            cand = " ".join(words[i : i + w])
            sm.set_seq1(cand)
            if sm.real_quick_ratio() < best_r or sm.quick_ratio() < best_r:
                continue
            r = sm.ratio()
            if r > best_r:
                best, best_r = cand, r
    return best, best_r


def verify_quote(conn: sqlite3.Connection, ds: str, ref: str, quote: str, pdf_dir: Path | None = None) -> QuoteCheck:
    """Check that ``quote`` occurs verbatim in unit ``ref`` of sheet ``ds`` and in the PDF text layer."""
    uid = parse_ref(ref)
    res = QuoteCheck(False, False, None, quote=quote, requested_ref=uid)
    sheet = conn.execute("SELECT * FROM documents WHERE doc_id=?", (ds,)).fetchone()
    if not sheet:
        res.message = f"{C.doc_label(ds)} is not indexed"
        return res
    qk = match_key(quote)
    if len(qk) < 8:
        res.message = "quote too short to verify (need at least 8 significant characters)"
        return res
    ctx = _Ctx(conn)
    urow = conn.execute(
        "SELECT * FROM units WHERE doc_id=? AND unit_id=? ORDER BY dup_index LIMIT 1", (ds, uid)
    ).fetchone()
    if urow is None:
        res.message = f"{C.doc_label(ds)} has no unit {uid} in the current edition"
    else:
        res.evidence = ctx.evidence(urow, text_limit=500)
        all_text = " ".join(
            r["text"] or "" for r in conn.execute("SELECT text FROM units WHERE doc_id=? AND unit_id=?", (ds, uid))
        )
        res.in_unit = qk in match_key(all_text)
    # --- which units contain it (all if the stated one does not)
    if not res.in_unit:
        for r in conn.execute("SELECT unit_id, text FROM units WHERE doc_id=? AND dup_index=0 ORDER BY seq", (ds,)):
            if qk in match_key(r["text"] or "") and r["unit_id"] != uid:
                res.found_in_units.append(r["unit_id"])
        # neighbouring-unit text sometimes splits a quote: try the whole sheet text as one
        res.found_in_units = res.found_in_units[:10]
    # --- PDF text layer
    pdf = Path(sheet["source_path"]) if sheet["source_path"] else None
    if not (pdf and pdf.exists()):
        alt = (pdf_dir or C.DEFAULT_PDF_DIR) / sheet["source_pdf"]
        pdf = alt if alt and alt.exists() else None
    if pdf is None:
        res.pdf_message = f"source PDF not found ({sheet['source_path']})"
    else:
        try:
            text, starts = _pdf_index(pdf, sheet["title"] or "", ds)
        except Exception as e:  # noqa: BLE001 - unreadable PDF: say so, do not guess
            text, starts = "", []
            res.pdf_message = f"cannot read the PDF text layer: {type(e).__name__}"
        else:
            res.pdf_pages = _pages_of(text, starts, qk)
            res.in_pdf = bool(res.pdf_pages)
        if sheet["source_sha256"] and text:
            from .convert import sha256_file

            if sha256_file(pdf) != sheet["source_sha256"]:
                res.pdf_message = "WARNING: PDF on disk differs from the indexed edition (sha256 mismatch)"
    res.match = bool(res.in_unit and res.in_pdf)
    if not res.match:
        base_txt = (
            " ".join(
                r["text"] or "" for r in conn.execute("SELECT text FROM units WHERE doc_id=? AND unit_id=?", (ds, uid))
            )
            if urow
            else ""
        )
        cand, ratio = _closest(base_txt, quote) if base_txt else ("", 0.0)
        cuid = uid if cand else None
        if ratio < 0.9:  # also look at the rest of the sheet
            for r in conn.execute("SELECT unit_id, text FROM units WHERE doc_id=? AND dup_index=0", (ds,)):
                if r["unit_id"] == uid or len(r["text"] or "") < 10:
                    continue
                c2, r2 = _closest(r["text"] or "", quote)
                if r2 > ratio:
                    cand, ratio, cuid = c2, r2, r["unit_id"]
        res.closest, res.closest_ratio, res.closest_unit_id = (cand or None), round(ratio, 3), cuid
        why = []
        if urow is not None and not res.in_unit:
            why.append(
                "not in the stated unit"
                + (f" (it is in: {', '.join(res.found_in_units)})" if res.found_in_units else "")
            )
        if res.in_pdf is False:
            why.append("not found in the PDF text layer")
        res.message = "; ".join(why) or res.message
    else:
        res.message = "verbatim in the unit and in the PDF text layer"
    del ctx
    return res
