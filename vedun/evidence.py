"""The single typed ``Evidence`` record returned by every lookup / section / search / figure call.

No result is ever returned without provenance. A field that is unknown is ``None`` and listed in
``null_reasons`` with the reason; nothing is omitted silently. Page numbers are internal
locators only (for rendering PNGs / opening the PDF); they are not part of any key or citation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config as C
from .convert import edition_tag
from .units import ancestors


class _Dict:
    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable dict."""
        return asdict(self)  # type: ignore[call-overload]


@dataclass
class Retrieval(_Dict):
    """How a result was found and ranked (all scores that contributed)."""

    match_type: str = "lookup"  # exact | search | section | lookup | figure
    final_ranker: str = "exact"  # exact | bm25 | vector:<model> | wrrf | wrrf+rerank:<key>
    fusion: str | None = None
    w_fts: float | None = None
    w_vec: float | None = None
    final_rank: int | None = None
    bm25: float | None = None  # positive, higher = better (negated FTS5 bm())
    bm25_rank: int | None = None
    vector: dict[str, dict[str, float | int]] = field(default_factory=dict)  # model -> distance/similarity/rank
    rrf: float | None = None
    rerank: dict[str, float] = field(default_factory=dict)  # reranker key -> relevance score
    rerank_error: str | None = None


@dataclass
class Evidence(_Dict):
    """Verbatim unit text plus complete document, unit, retrieval and citation provenance."""

    # --- document
    doc_id: str
    document_title: str | None
    edition: str
    edition_label: str | None
    edition_label_source: str | None
    source_pdf: str | None  # absolute path
    source_sha256: str | None
    source_item_id: str | None
    indexed_at: str | None
    # --- unit
    unit_id: str
    unit_kind: str
    title: str
    parent_path: list[dict[str, str]]
    page_start: int | None  # internal locator only, not a key or citation element
    page_end: int | None
    md_path: str | None
    md_line_start: int | None
    md_line_end: int | None
    n_same_id: int
    # --- evidence
    text: str  # VERBATIM unit text as extracted (never paraphrased, nothing generated)
    text_chars: int
    text_truncated: bool
    text_source: str
    snippet: str | None
    figure_png: str | None
    page_png: str | None
    # --- retrieval
    retrieval: Retrieval
    # --- citation
    citation_pl: str
    citation_en: str
    null_reasons: dict[str, str]

    # convenience for callers/tests that treat results as hits
    @property
    def score(self) -> float | None:
        return self.retrieval.rrf


_ID_PREFIX = {"table": "Table ", "figure": "Figure ", "appendix": "Appendix "}


def locator(kind: str, unit_id: str, lang: str) -> str:
    """``pkt 2.3.1.4`` / ``Tab. 2.4.1.1`` / ``rys. 2.4.3`` (PL); words from the corpus profile (``locators``)."""
    words = C.LOCATORS.get(lang) or C.LOCATORS["en"]
    k = kind if kind in _ID_PREFIX else "clause"
    return words[k].format(id=unit_id.removeprefix(_ID_PREFIX.get(k, "")).strip())


def make_citation(
    ds: str, title: str | None, label: str | None, edition: str, kind: str, unit_id: str, lang: str
) -> str:
    """Ready-to-paste citation without page numbers, from the profile's ``citation_template``."""
    template = C.CITATION_TEMPLATE.get(lang) or C.CITATION_TEMPLATE["en"]
    return template.format(
        doc_id=ds, title=title or "?", edition=label or edition, locator=locator(kind, unit_id, lang)
    )


def evidence_from_rows(
    conn: sqlite3.Connection,
    u: sqlite3.Row,
    sheet: sqlite3.Row,
    *,
    retrieval: Retrieval | None = None,
    snippet: str | None = None,
    text_limit: int | None = None,
    n_same_id: int = 1,
    fig_dir: Path | None = None,
    md_dir: Path | None = None,
    pdf_dir: Path | None = None,
    with_images: bool = False,
    known_ids: dict[str, set[str]] | None = None,
) -> Evidence:
    """Build an :class:`Evidence` from a ``units`` row and its ``documents`` row."""
    import json

    fig_dir = fig_dir or C.FIG_DIR
    md_dir = md_dir or C.MD_DIR
    ds = u["doc_id"]
    ed = u["edition"]
    extra = json.loads(u["extra"] or "{}")
    why: dict[str, str] = {}
    if known_ids is None:
        known_ids = {}
    if ds not in known_ids:
        known_ids[ds] = {r[0] for r in conn.execute("SELECT unit_id FROM units WHERE doc_id=?", (ds,))}
    kind = u["kind"]
    anchor = u["unit_id"] if kind == "clause" else (u["parent"] or "")
    chain = ancestors(known_ids[ds], anchor) + ([anchor] if anchor and anchor in known_ids[ds] else [])
    if kind in ("table", "figure") and not chain:
        why["parent_path"] = "table/figure appears before any numbered clause"
    titles = {}
    if chain:
        q = ",".join("?" * len(chain))
        titles = {
            r[0]: r[1]
            for r in conn.execute(
                f"SELECT unit_id, title FROM units WHERE doc_id=? AND dup_index=0 AND unit_id IN ({q})", [ds, *chain]
            )
        }
    parent_path = [{"unit_id": c, "title": titles.get(c, "")} for c in chain if c != u["unit_id"]]
    text = u["text"] or ""
    truncated = bool(text_limit and len(text) > text_limit)
    shown = text[:text_limit] if truncated else text
    md_file = sheet["md_file"]
    md_path = str(md_dir / md_file) if md_file else None
    tag = edition_tag(ed)
    figure_png = page_png = None
    if kind == "figure":
        f = extra.get("file")
        p = fig_dir / f"{ds}_{tag}" / f if f else None
        if p and p.exists():
            figure_png = str(p)
        else:
            why["figure_png"] = (
                "figure image file missing on disk" if f else "no image crop was produced for this figure"
            )
    else:
        why["figure_png"] = "not a figure unit"
    if with_images and u["page_start"]:
        from .search import get_page_image

        r = get_page_image(conn, ds, int(u["page_start"]), pdf_dir=pdf_dir, fig_dir=fig_dir)
        if r.ok:
            page_png = r.path
        else:
            why["page_png"] = r.message
    else:
        why["page_png"] = "not rendered (pass with_images=True or call get_page_image)"
    if not sheet["source_item_id"]:
        why["source_item_id"] = "not populated yet (to be filled from the source catalog)"
    if not sheet["edition_label"]:
        why["edition_label"] = "no label stored"
    if snippet is None:
        why["snippet"] = "not a search result"
    title_s = sheet["title"]
    label = sheet["edition_label"]
    return Evidence(
        doc_id=ds,
        document_title=title_s,
        edition=ed,
        edition_label=label,
        edition_label_source=sheet["edition_label_source"],
        source_pdf=C.pdf_path(sheet["source_pdf"], sheet["source_path"]),
        source_sha256=sheet["source_sha256"],
        source_item_id=sheet["source_item_id"],
        indexed_at=sheet["indexed_at"],
        unit_id=u["unit_id"],
        unit_kind=kind,
        title=u["title"] or "",
        parent_path=parent_path,
        page_start=u["page_start"],
        page_end=u["page_end"],
        md_path=md_path,
        md_line_start=u["md_line_start"],
        md_line_end=u["md_line_end"],
        n_same_id=n_same_id,
        text=shown,
        text_chars=len(text),
        text_truncated=truncated,
        text_source=(
            "verbatim from Docling extraction of the PDF (Markdown file lines "
            f"{u['md_line_start']}-{u['md_line_end']}); confirm with verify_quote()"
        ),
        snippet=snippet,
        figure_png=figure_png,
        page_png=page_png,
        retrieval=retrieval or Retrieval(),
        citation_pl=make_citation(ds, title_s, label, ed, kind, u["unit_id"], "pl"),
        citation_en=make_citation(ds, title_s, label, ed, kind, u["unit_id"], "en"),
        null_reasons=why,
    )
