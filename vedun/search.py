"""Retrieval API of the knowledge base (future MCP tool list, one function per tool).

* :func:`lookup`      exact by sheet + clause/table/figure id (forgiving input), never fuzzy
* :func:`toc`         browse the structure of a sheet or branch (light records)
* :func:`fulltext`    FTS5/BM25 only: phrases, prefix*, AND/OR/NOT, NEAR() - never touches vectors
* :func:`search`      blind/semantic: weighted RRF of BM25 + vectors (+ figures) + optional reranker
* :func:`figure`, :func:`page_image`, :func:`list_documents`
  (:func:`vedun.quote.verify_quote` and :func:`vedun.check_citations.check_citations` complete the set)

Everything returns typed dataclasses (``.to_dict()`` is JSON-safe); every hit is an
:class:`vedun.evidence.Evidence` with full provenance. Nothing is guessed.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import sqlite_vec

from . import config as C
from .convert import edition_tag
from .embed import Embedder, get_embedder
from .evidence import Evidence, Retrieval, evidence_from_rows
from .rerank import Reranker, get_reranker
from .units import ID_TOKEN, TABLE_TOKEN, ancestors, is_clause_descendant, sort_key

Mode = Literal["hybrid", "bm25", "vector"]
MAX_LIMIT = 50
TEXT_LIMIT_HIT = 1500  # characters of unit text embedded in a search hit (full text via lookup)
CANDIDATES = 60  # per-retriever depth before fusion
EmbedderFactory = Callable[[str], Embedder]
RerankerFactory = Callable[[str | None], Reranker | None]


class _Dict:
    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable dict."""
        return asdict(self)  # type: ignore[call-overload]


# ============================================================================ id parsing
_CLAUSE_PREFIX = re.compile(r"^(?:pkt\.?|punkt\w*|sec(?:tion|t)?\.?|clause|ust\.?)\s*", re.I)
_TABLE_PREFIX = re.compile(r"^(?:tab(?:le|ela|\.)?|tabl\.?)\s*", re.I)
_FIG_PREFIX = re.compile(r"^(?:fig(?:ure|\.)?|rys(?:unek|\.)?)\s*", re.I)
_APP_PREFIX = re.compile(r"^(?:appendix|zał(?:ącznik|\.)?|zal(?:acznik|\.)?)\s*", re.I)


def parse_ref(raw: str) -> str:
    """Forgiving id parser: ``pkt 2.3.1.4`` / ``Tab. 2.4.1.1`` / ``rys. 2.4.3`` -> canonical id."""
    s = raw.strip().strip("*_`").strip()
    if m := _TABLE_PREFIX.match(s):
        return "Table " + _trim_ref(s[m.end() :])
    if m := _FIG_PREFIX.match(s):
        return "Figure " + _trim_ref(s[m.end() :])
    if m := _APP_PREFIX.match(s):
        rest = s[m.end() :].strip()
        return "Appendix " + (rest[:1].upper() if rest else "")
    return _trim_ref(_CLAUSE_PREFIX.sub("", s))


def _trim_ref(s: str) -> str:
    """Strip trailing punctuation; a closing parenthesis only when unbalanced ("2.4.1(a)" keeps it)."""
    s = s.strip().rstrip(".,;:").strip()
    while s.endswith(")") and s.count(")") > s.count("("):
        s = s[:-1].rstrip(".,;:").strip()
    return s


# ============================================================================ dataclasses
@dataclass
class LookupItem(_Dict):
    """Result for one requested reference."""

    requested: str
    found: bool
    evidence: Evidence | None = None
    duplicates: list[Evidence] = field(default_factory=list)  # the source repeats the id (typo in the source)
    children: list[Evidence] = field(default_factory=list)
    prev_id: str | None = None
    next_id: str | None = None
    nearest: list[str] = field(default_factory=list)
    possibly_renumbered_to: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class LookupResponse(_Dict):
    """Result of :func:`lookup`."""

    doc_id: str
    document_indexed: bool
    items: list[LookupItem] = field(default_factory=list)
    message: str = ""

    @property
    def found(self) -> bool:
        return bool(self.items) and all(i.found for i in self.items)

    @property
    def first(self) -> Evidence | None:
        return self.items[0].evidence if self.items else None


@dataclass
class SectionResult(_Dict):
    """Clause with children as one text (``get_section``)."""

    found: bool
    doc_id: str
    unit_id: str
    units: list[Evidence] = field(default_factory=list)
    text: str = ""
    truncated: bool = False
    message: str = ""


@dataclass
class TocItem(_Dict):
    """Light structure record: ids and headings, no full text."""

    doc_id: str
    edition: str
    unit_id: str
    kind: str
    title: str
    depth: int
    n_children: int
    parent: str | None


@dataclass
class TocResponse(_Dict):
    """Result of :func:`toc`."""

    doc_id: str
    edition: str | None
    document_title: str | None
    edition_label: str | None
    prefix: str | None
    found: bool
    items: list[TocItem] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    message: str = ""


@dataclass
class FulltextResponse(_Dict):
    """Result of :func:`fulltext` (FTS5/BM25 only)."""

    ok: bool
    query: str
    syntax: str
    match_expression: str
    hits: list[Evidence] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    error: str | None = None


@dataclass
class SearchResponse(_Dict):
    """Result of :func:`search`."""

    query: str
    mode: str
    fusion: str
    w_fts: float
    w_vec: float
    reranker: str | None
    hits: list[Evidence] = field(default_factory=list)
    total_candidates: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    notes: list[str] = field(default_factory=list)  # degradations, e.g. "vector retrieval failed: ..."


@dataclass
class DocumentInfo(_Dict):
    """One indexed document."""

    doc_id: str
    title: str
    edition: str
    edition_label: str | None
    pages: int
    n_units: int
    n_chunks: int
    n_figures: int
    source_pdf: str
    source_path: str | None
    source_sha256: str
    indexed_at: str | None
    source_item_id: str | None
    models: dict[str, str] = field(default_factory=dict)


@dataclass
class ImageResult(_Dict):
    """A figure or page image on disk."""

    ok: bool
    doc_id: str
    ref: str
    path: str | None = None
    caption: str = ""
    page: int | None = None
    evidence: Evidence | None = None
    message: str = ""


# ============================================================================ helpers
class _Ctx:
    """Per-call caches (sheet rows, id sets) so building many Evidence records stays cheap."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        with_images: bool = False,
        fig_dir: Path | None = None,
        md_dir: Path | None = None,
        pdf_dir: Path | None = None,
    ) -> None:
        self.conn = conn
        self.with_images = with_images
        self.fig_dir, self.md_dir, self.pdf_dir = fig_dir, md_dir, pdf_dir
        self._sheets: dict[str, sqlite3.Row] = {}
        self.known: dict[str, set[str]] = {}

    def sheet(self, ds: str) -> sqlite3.Row:
        if ds not in self._sheets:
            self._sheets[ds] = self.conn.execute("SELECT * FROM documents WHERE doc_id=?", (ds,)).fetchone()
        return self._sheets[ds]

    def evidence(
        self,
        u: sqlite3.Row,
        *,
        retrieval: Retrieval | None = None,
        snippet: str | None = None,
        text_limit: int | None = None,
        n_same: int = 1,
    ) -> Evidence:
        return evidence_from_rows(
            self.conn,
            u,
            self.sheet(u["doc_id"]),
            retrieval=retrieval,
            snippet=snippet,
            text_limit=text_limit,
            n_same_id=n_same,
            fig_dir=self.fig_dir,
            md_dir=self.md_dir,
            pdf_dir=self.pdf_dir,
            with_images=self.with_images,
            known_ids=self.known,
        )


def _as_list(v: str | Sequence[str] | None) -> list[str] | None:
    if v is None:
        return None
    return [v] if isinstance(v, str) else list(v)


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _plain_snippet(text: str, query: str, width: int = 300) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= width:
        return t
    words = {w.lower() for w in re.findall(r"[^\W_]{3,}", query)}
    best = 0
    for m in re.finditer(r"[^\W_]{3,}", t):
        if m.group(0).lower() in words:
            best = max(0, m.start() - width // 4)
            break
    return ("…" if best else "") + t[best : best + width] + ("…" if best + width < len(t) else "")


def _fts_snippet(conn: sqlite3.Connection, match: str, rowid: int) -> str | None:
    """Highlighted snippet (``**term**``) of the unit text via FTS5 ``snippet()``."""
    if not match:
        return None
    try:
        r = conn.execute(
            "SELECT snippet(units_fts, 1, '**', '**', '…', 40) FROM units_fts WHERE units_fts MATCH ? AND rowid = ?",
            (match, rowid),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return r[0] if r and r[0] else None


# ============================================================================ FTS5 queries
_FTS_KEYWORDS = {"and", "or", "not", "near"}
_STOPWORDS = set(
    "a an the of for to in on at by and or is are be with from as that this it its how what which when where "
    "often all any each than then there their "
    "w we na z ze do od dla i o po przy pod nad oraz lub jak czy jest są się być nie to że przez między "
    "jednego jednej jeden jedna co ma mają czego kiedy gdzie który która które".split()
)


def significant_terms(query: str) -> list[str]:
    """Lower-cased content words of a query (stop words and 1-letter tokens removed)."""
    words = [w.lower() for w in re.findall(r"[^\W_]+(?:[.\-][^\W_]+)*", query, re.UNICODE)]
    sig = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    return sig or words


def _term_in(term: str, tokens: set[str]) -> bool:
    if term in tokens:
        return True
    stem = term[: max(4, len(term) - 3)] if len(term) > 5 else term
    return any(t.startswith(stem) for t in tokens) if len(stem) >= 4 else False


def coverage_gate(
    conn: sqlite3.Connection, query: str, ranked: list[tuple[int, float]], min_cov: float = 0.3
) -> list[tuple[int, float]]:
    """Drop BM25 hits that contain less than ``min_cov`` of the query's content words.

    OR-ed BM25 returns noise for queries in another language (a Polish question against an
    English corpus matches only stray words); such noise must not win rank-fusion votes.
    """
    terms = significant_terms(query)
    if not ranked or not terms:
        return ranked
    q = ",".join("?" * len(ranked))
    rows = {
        r[0]: r for r in conn.execute(f"SELECT id, title, text FROM units WHERE id IN ({q})", [u for u, _ in ranked])
    }
    need = max(1, -(-len(terms) * min_cov // 1)) if len(terms) > 2 else len(terms)
    out = []
    for uid, score in ranked:
        toks = set(re.findall(r"[^\W_]+(?:[.\-][^\W_]+)*", f"{rows[uid][1]} {rows[uid][2]}".lower(), re.UNICODE))
        toks |= {p for t in list(toks) for p in re.split(r"[.\-]", t)}
        if sum(_term_in(t, toks) for t in terms) >= need:
            out.append((uid, score))
    return out


def sanitize_fts(query: str, max_terms: int = 24, operator: str = "OR", drop_stop: bool = False) -> str:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Only letters/digits survive inside terms, so FTS5 operators can never reach the parser.
    Balanced ``"double quoted"`` phrases are kept as phrases. A dotted number such as
    ``2.3.1.4`` becomes the phrase ``"2 3 1 4"`` (how unicode61 tokenises it).
    ``operator`` joins the terms (``OR`` for recall in hybrid search, ``AND`` for fulltext).
    """
    terms: list[str] = []
    for m in re.finditer(r'"([^"]*)"|([^\W_]+(?:[.\-/][^\W_]+)*)', query, re.UNICODE):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        parts = re.findall(r"[^\W_]+", raw, re.UNICODE)
        if not parts:
            continue
        if m.group(1) is None and len(parts) == 1 and parts[0].lower() in _FTS_KEYWORDS:
            continue
        if drop_stop and m.group(1) is None and len(parts) == 1 and parts[0].lower() in _STOPWORDS:
            continue
        terms.append('"' + " ".join(parts) + '"')
        if len(terms) >= max_terms:
            break
    if not terms and drop_stop:  # query made only of stop words: fall back to all words
        return sanitize_fts(query, max_terms, operator, drop_stop=False)
    return f" {operator} ".join(dict.fromkeys(terms))


def validate_fts5(conn: sqlite3.Connection, expr: str) -> str | None:
    """Return None if ``expr`` is a valid FTS5 expression, else the parser's error message."""
    try:
        conn.execute("SELECT count(*) FROM units_fts WHERE units_fts MATCH ?", (expr,)).fetchall()
    except sqlite3.OperationalError as e:
        return str(e)
    return None


def weighted_rrf(rank_lists: Sequence[tuple[Sequence[Any], float]], k: int = C.RRF_K) -> dict[Any, float]:
    """Weighted Reciprocal Rank Fusion: ``score(d) = sum_i w_i / (k + rank_i(d))`` (rank from 1).

    A document absent from a list contributes 0 from that list.
    """
    scores: dict[Any, float] = {}
    for lst, w in rank_lists:
        for rank, item in enumerate(lst, start=1):
            scores[item] = scores.get(item, 0.0) + w / (k + rank)
    return scores


def rrf(rank_lists: list[list[Any]], k: int = 60) -> dict[Any, float]:
    """Unweighted RRF (all weights 1)."""
    return weighted_rrf([(lst, 1.0) for lst in rank_lists], k)


def _where(ds: list[str] | None, kinds: list[str] | None) -> tuple[str, list[str]]:
    sql, args = "", []
    if ds:
        sql += f" AND u.doc_id IN ({','.join('?' * len(ds))})"
        args += ds
    if kinds:
        sql += f" AND u.kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    return sql, args


def bm25_ranked(
    conn: sqlite3.Connection,
    match: str,
    limit: int,
    ds: list[str] | None = None,
    kinds: list[str] | None = None,
    offset: int = 0,
) -> list[tuple[int, float]]:
    """``(unit row id, score)`` ranked by BM25; score is positive, higher = better."""
    if not match:
        return []
    w, a = _where(ds, kinds)
    sql = (
        "SELECT u.id, bm25(units_fts, 4.0, 1.0, 6.0) FROM units_fts f JOIN units u ON u.id=f.rowid "
        f"WHERE units_fts MATCH ?{w} ORDER BY bm25(units_fts, 4.0, 1.0, 6.0) LIMIT ? OFFSET ?"
    )
    return [(r[0], -float(r[1])) for r in conn.execute(sql, [match, *a, limit, offset])]


def vector_ranked(
    conn: sqlite3.Connection,
    qvec: list[float],
    model_key: str,
    limit: int,
    ds: list[str] | None = None,
    kinds: list[str] | None = None,
) -> list[tuple[int, float]]:
    """``(unit row id, cosine distance)`` by best chunk of each unit."""
    from .db import vec_table

    over = limit * 4 if (ds and len(ds) != 1) or kinds else limit * 2
    args: list[Any] = [sqlite_vec.serialize_float32(qvec), over]
    where = "embedding MATCH ? AND k = ?"
    if ds and len(ds) == 1:
        where += " AND doc_id = ?"
        args.append(ds[0])
    rows = conn.execute(f"SELECT chunk_id, distance FROM {vec_table(model_key)} WHERE {where}", args).fetchall()
    if not rows:
        return []
    q = ",".join("?" * len(rows))
    cmap = {
        r[0]: r[1] for r in conn.execute(f"SELECT id, unit_row FROM chunks WHERE id IN ({q})", [r[0] for r in rows])
    }
    best: dict[int, float] = {}
    for cid, dist in rows:
        uid = cmap.get(cid)
        if uid is not None and (uid not in best or dist < best[uid]):
            best[uid] = dist
    ordered = sorted(best.items(), key=lambda kv: kv[1])
    if (ds and len(ds) > 1) or kinds:
        urows = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, doc_id, kind FROM units WHERE id IN ({','.join('?' * len(ordered))})",
                [u for u, _ in ordered],
            )
        }
        ordered = [
            (u, d)
            for u, d in ordered
            if (not ds or urows[u]["doc_id"] in ds) and (not kinds or urows[u]["kind"] in kinds)
        ]
    return ordered[:limit]


def figure_ranked(
    conn: sqlite3.Connection, qvec: list[float], model_key: str, limit: int, ds: list[str] | None = None
) -> list[tuple[int, float]]:
    """``(figure unit row id, cosine distance)`` from the image vectors of ``model_key``."""
    from .db import figvec_table

    rows = conn.execute(
        f"SELECT unit_row, distance FROM {figvec_table(model_key)} WHERE embedding MATCH ? AND k = ?",
        [sqlite_vec.serialize_float32(qvec), limit * 3 if ds else limit],
    ).fetchall()
    out = [(r[0], r[1]) for r in rows]
    if ds and out:
        urows = {
            r["id"]: r["doc_id"]
            for r in conn.execute(
                f"SELECT id, doc_id FROM units WHERE id IN ({','.join('?' * len(out))})", [u for u, _ in out]
            )
        }
        out = [(u, d) for u, d in out if urows[u] in ds]
    return out[:limit]


# ============================================================================ lookup
def _ids(conn: sqlite3.Connection, ds: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT unit_id FROM units WHERE doc_id=?", (ds,))]


def nearest_ids(ids: list[str], wanted: str, n: int = 6) -> list[str]:
    """Existing ids closest to ``wanted``: longest shared dotted prefix, then string similarity."""
    is_tf = wanted.startswith(("Table", "Figure"))
    wp = wanted.split(" ")[-1].split(".")

    def shared(i: str) -> int:
        k = 0
        for a, b in zip(wp, i.split(" ")[-1].split("."), strict=False):
            if a != b:
                break
            k += 1
        return k

    pool = [
        i
        for i in ids
        if i.startswith(("Table", "Figure")) == is_tf and (not is_tf or i.split(" ")[0] == wanted.split(" ")[0])
    ]
    pool.sort(key=lambda i: (-shared(i), -difflib.SequenceMatcher(None, i, wanted).ratio(), sort_key(i)))
    return pool[:n]


def _siblings(conn: sqlite3.Connection, ds: str, unit_id: str) -> tuple[str | None, str | None]:
    """Previous / next unit id in document order among units of the same kind and parent."""
    r = conn.execute(
        "SELECT seq, kind, parent FROM units WHERE doc_id=? AND unit_id=? AND dup_index=0", (ds, unit_id)
    ).fetchone()
    if not r:
        return None, None
    prev = conn.execute(
        "SELECT unit_id FROM units WHERE doc_id=? AND kind=? AND seq<? AND dup_index=0 ORDER BY seq DESC LIMIT 1",
        (ds, r["kind"], r["seq"]),
    ).fetchone()
    nxt = conn.execute(
        "SELECT unit_id FROM units WHERE doc_id=? AND kind=? AND seq>? AND dup_index=0 ORDER BY seq LIMIT 1",
        (ds, r["kind"], r["seq"]),
    ).fetchone()
    return (prev[0] if prev else None), (nxt[0] if nxt else None)


def _descendant_rows(conn: sqlite3.Connection, ds: str, u: sqlite3.Row) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM units WHERE doc_id=? AND dup_index=0 ORDER BY seq", (ds,)).fetchall()
    if u["kind"] != "clause":
        return []
    cids = {r["unit_id"] for r in rows if r["kind"] == "clause" and is_clause_descendant(r["unit_id"], u["unit_id"])}
    return [
        r
        for r in rows
        if (r["kind"] == "clause" and r["unit_id"] in cids)
        or (
            r["kind"] in ("table", "figure", "appendix")
            and r["parent"]
            and (r["parent"] == u["unit_id"] or r["parent"] in cids)
        )
    ]


def lookup(
    conn: sqlite3.Connection,
    ds: str,
    ref: str | Sequence[str],
    *,
    include_children: bool = False,
    neighbors: bool = False,
    max_chars: int | None = None,
    with_images: bool = False,
    ctx: _Ctx | None = None,
) -> LookupResponse:
    """Exact lookup by sheet + id (forgiving input). Unknown ids give ``found=False`` + nearest ids.

    ``include_children`` adds all descendants (clauses, tables, figures); ``neighbors`` adds the
    previous/next sibling ids. No fuzzy text search happens here.
    """
    refs = [ref] if isinstance(ref, str) else list(ref)
    ctx = ctx or _Ctx(conn, with_images=with_images)
    if not conn.execute("SELECT 1 FROM documents WHERE doc_id=?", (ds,)).fetchone():
        return LookupResponse(
            ds,
            False,
            [LookupItem(r, False, message=f"{C.doc_label(ds)} is not indexed") for r in refs],
            f"{C.doc_label(ds)} is not indexed",
        )
    items: list[LookupItem] = []
    for raw in refs:
        uid = parse_ref(raw)
        rows = conn.execute("SELECT * FROM units WHERE doc_id=? AND unit_id=? ORDER BY dup_index", (ds, uid)).fetchall()
        if not rows:
            ren = [
                r[0]
                for r in conn.execute(
                    "SELECT new_id FROM renames WHERE doc_id=? AND old_id=? ORDER BY new_edition DESC", (ds, uid)
                )
            ]
            items.append(
                LookupItem(
                    raw,
                    False,
                    nearest=nearest_ids(_ids(conn, ds), uid),
                    possibly_renumbered_to=ren,
                    message=f"{C.doc_label(ds)} has no unit {uid} in the current edition",
                )
            )
            continue
        n = len(rows)
        ev = ctx.evidence(rows[0], retrieval=Retrieval("lookup", "exact"), text_limit=max_chars, n_same=n)
        item = LookupItem(
            raw,
            True,
            ev,
            duplicates=[
                ctx.evidence(r, retrieval=Retrieval("lookup", "exact"), text_limit=max_chars, n_same=n)
                for r in rows[1:]
            ],
        )
        if n > 1:
            item.message = f"the source itself repeats id {uid} {n} times (numbering error in the source)"
        if include_children:
            item.children = [
                ctx.evidence(r, retrieval=Retrieval("lookup", "exact"), text_limit=max_chars)
                for r in _descendant_rows(conn, ds, rows[0])
            ]
        if neighbors:
            item.prev_id, item.next_id = _siblings(conn, ds, uid)
        items.append(item)
    return LookupResponse(ds, True, items)


def get_section(
    conn: sqlite3.Connection, ds: str, unit_id: str, include_children: bool = True, max_chars: int = 20000
) -> SectionResult:
    """A clause with its descendants as one text, capped at ``max_chars`` (verbatim pieces)."""
    r = lookup(conn, ds, unit_id, include_children=include_children)
    it = r.items[0]
    if not it.found or it.evidence is None:
        return SectionResult(False, ds, parse_ref(unit_id), message=it.message)
    units = [it.evidence, *it.children]
    out, used, truncated = [], 0, False
    for u in units:
        piece = u.text if u.unit_kind != "table" else f"{u.title}\n{u.text}"
        if used + len(piece) > max_chars:
            out.append(piece[: max(0, max_chars - used)])
            truncated = True
            break
        out.append(piece)
        used += len(piece) + 2
    return SectionResult(True, ds, it.evidence.unit_id, units, "\n\n".join(out), truncated)


# ============================================================================ toc
def toc(
    conn: sqlite3.Connection,
    ds: str,
    prefix: str | None = None,
    depth: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> TocResponse:
    """Structure of a sheet or branch (ids, kinds, headings, child counts; no text).

    ``depth`` is relative to ``prefix`` (1 = direct children only); ``None`` = whole branch.
    Without ``prefix`` the whole sheet is listed.
    """
    s = conn.execute("SELECT * FROM documents WHERE doc_id=?", (ds,)).fetchone()
    if not s:
        return TocResponse(ds, None, None, None, prefix, False, message=f"{C.doc_label(ds)} is not indexed")
    limit = max(1, min(int(limit), 500))
    rows = conn.execute(
        "SELECT unit_id, kind, title, parent, seq FROM units WHERE doc_id=? AND dup_index=0 ORDER BY seq", (ds,)
    ).fetchall()
    known = {r["unit_id"] for r in rows if r["kind"] == "clause"}
    level: dict[str, int] = {}
    par: dict[str, str | None] = {}
    for r in rows:
        if r["kind"] == "clause":
            anc = ancestors(known, r["unit_id"])
            level[r["unit_id"]] = len(anc)
            par[r["unit_id"]] = anc[-1] if anc else None
        else:
            # a table or figure hangs under its clause or appendix (appendices themselves are top level)
            p = r["parent"] if r["parent"] in known or r["parent"] in level else None
            level[r["unit_id"]] = (level.get(p, -1) + 1) if p else 0
            par[r["unit_id"]] = p
    nkids: dict[str, int] = {}
    for p in par.values():
        if p:
            nkids[p] = nkids.get(p, 0) + 1
    root = parse_ref(prefix) if prefix else None
    if root and root not in level:
        return TocResponse(
            ds,
            s["edition"],
            s["title"],
            s["edition_label"],
            root,
            False,
            message=f"{C.doc_label(ds)} has no unit {root}; nearest: {', '.join(nearest_ids(list(level), root))}",
        )
    sel = []
    for r in rows:
        uid = r["unit_id"]
        if root is None:
            rel = level[uid]
            ok = True
        else:
            in_branch = (
                uid == root
                or (r["kind"] == "clause" and is_clause_descendant(uid, root))
                or (r["kind"] != "clause" and par[uid] and (par[uid] == root or is_clause_descendant(par[uid], root)))
            )
            ok = bool(in_branch)
            rel = level[uid] - level[root]
        if ok and (depth is None or rel <= depth):
            sel.append(TocItem(ds, s["edition"], uid, r["kind"], r["title"] or "", rel, nkids.get(uid, 0), par[uid]))
    page = sel[offset : offset + limit]
    return TocResponse(
        ds,
        s["edition"],
        s["title"],
        s["edition_label"],
        root,
        True,
        page,
        len(sel),
        offset,
        limit,
        offset + limit < len(sel),
    )


# ============================================================================ fulltext
def fulltext(
    conn: sqlite3.Connection,
    query: str,
    ds: str | Sequence[str] | None = None,
    kind: str | Sequence[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    syntax: Literal["safe", "fts5"] = "safe",
    with_images: bool = False,
) -> FulltextResponse:
    """FTS5/BM25 search only (no vectors, no reranker, no network).

    ``syntax="safe"`` (default) escapes the text into plain AND-ed terms (balanced ``"quotes"``
    stay phrases). ``syntax="fts5"`` passes the expression through after validation: phrases,
    ``prefix*``, ``AND``/``OR``/``NOT``, ``NEAR(a b, 5)``; a malformed expression returns
    ``ok=False`` with the parser's message instead of raising.
    """
    limit = _clamp(limit)
    dsl, kinds = _as_list(ds), _as_list(kind)
    expr = sanitize_fts(query, operator="AND") if syntax == "safe" else query.strip()
    resp = FulltextResponse(False, query, syntax, expr, limit=limit, offset=offset)
    if not expr:
        resp.error = "empty query"
        return resp
    err = validate_fts5(conn, expr)
    if err:
        resp.error = f"invalid FTS5 expression: {err}"
        return resp
    w, a = _where(dsl, kinds)
    resp.total = conn.execute(
        f"SELECT COUNT(*) FROM units_fts f JOIN units u ON u.id=f.rowid WHERE units_fts MATCH ?{w}", [expr, *a]
    ).fetchone()[0]
    ranked = bm25_ranked(conn, expr, limit, dsl, kinds, offset)
    ctx = _Ctx(conn, with_images=with_images)
    for i, (uid, score) in enumerate(ranked, start=offset + 1):
        u = conn.execute("SELECT * FROM units WHERE id=?", (uid,)).fetchone()
        snip = _fts_snippet(conn, expr, uid) or _plain_snippet(u["text"] or "", query)
        resp.hits.append(
            ctx.evidence(
                u,
                retrieval=Retrieval("search", "bm25", final_rank=i, bm25=score, bm25_rank=i),
                snippet=snip,
                text_limit=TEXT_LIMIT_HIT,
            )
        )
    resp.ok = True
    resp.has_more = offset + limit < resp.total
    return resp


# ============================================================================ search
_TAB_RE = re.compile(rf"\b(?:tab(?:le|ela|\.)?)\s*({TABLE_TOKEN})", re.I)
_FIG_RE = re.compile(rf"\b(?:fig(?:ure|\.)?|rys(?:unek|\.)?)\s*({ID_TOKEN})", re.I)
_LOC_CLAUSE_RE = re.compile(r"\b(?:pkt\.?|punkt\w*|sec(?:tion|t)?\.?|clause)\s*(\d+(?:\.\d+)+)", re.I)
_BARE_CLAUSE_RE = re.compile(r"(?<![\d.,\-])(\d+(?:\.\d+){2,})(?![\d])")


def route_query(query: str) -> tuple[list[str], list[str]]:
    """Cheap deterministic routing: (sheet numbers, unit ids) mentioned in the query text."""
    dss = list(dict.fromkeys(re.findall(rf"(?<![\d.\-])({C.DOC_ID_PATTERN})(?![\d.\-])", query)))
    ids: list[str] = [f"Table {m}" for m in _TAB_RE.findall(query)]
    ids += [f"Figure {m}" for m in _FIG_RE.findall(query)]
    rest = _TAB_RE.sub(" ", _FIG_RE.sub(" ", query))  # ids already taken as tables/figures
    ids += _LOC_CLAUSE_RE.findall(rest)
    ids += [m for m in _BARE_CLAUSE_RE.findall(rest) if m not in ids]
    return dss, list(dict.fromkeys(ids))


def _doc_for_rerank(u: sqlite3.Row, ctx: _Ctx) -> str:
    s = ctx.sheet(u["doc_id"])
    return f"{C.doc_label(u['doc_id'])} {s['title']} - {u['unit_id']} {u['title'] or ''}\n{(u['text'] or '')[:2000]}"


def search(
    conn: sqlite3.Connection,
    query: str,
    ds: str | Sequence[str] | None = None,
    kind: str | Sequence[str] | None = None,
    *,
    w_fts: float | None = None,
    w_vec: float | None = None,
    rerank: str | None = "default",
    include_figures: bool = True,
    limit: int = 10,
    offset: int = 0,
    mode: Mode = "hybrid",
    model: str | None = None,
    figure_model: str | None = None,
    route_exact: bool = True,
    with_images: bool = False,
    embedder_factory: EmbedderFactory = get_embedder,
    reranker_factory: RerankerFactory = get_reranker,
    rerank_candidates: int | None = None,
) -> SearchResponse:
    """Blind/semantic search: weighted RRF of BM25 + vectors (+ figure vectors) + optional reranker.

    Pipeline: (1) exact-id shortcut (a sheet number and/or clause/table/figure id in the query puts
    that unit at rank 1 with ``match_type="exact"``); (2) BM25 and vector candidates, fused by
    ``score = w_fts/(k+rank_fts) + w_vec/(k+rank_vec)``; (3) the top ``rerank_candidates`` (30) are
    reordered by the reranker if one is configured; (4) ``offset``/``limit`` pagination.
    Retrieval failures degrade gracefully and are listed in ``notes`` (never silent).
    """
    limit = _clamp(limit)
    w_fts = C.W_FTS if w_fts is None else w_fts
    w_vec = C.W_VEC if w_vec is None else w_vec
    model = model or C.DEFAULT_TEXT_MODEL
    figure_model = figure_model or C.DEFAULT_IMAGE_MODEL
    rr_key = C.DEFAULT_RERANKER if rerank == "default" else rerank
    n_cand = rerank_candidates or C.RERANK_CANDIDATES
    dsl, kinds = _as_list(ds), _as_list(kind)
    resp = SearchResponse(query, mode, C.FUSION, w_fts, w_vec, rr_key, limit=limit, offset=offset)
    ctx = _Ctx(conn, with_images=with_images)
    base_ret = dict(fusion=C.FUSION, w_fts=w_fts, w_vec=w_vec)

    # (1) exact routing
    exact: list[Evidence] = []
    if route_exact:
        dss, ids = route_query(query)
        indexed = {r[0] for r in conn.execute("SELECT doc_id FROM documents")}
        targets = [d for d in dss if d in indexed] or (dsl or sorted(indexed) if ids else [])
        if ids and targets:
            for d in targets[:5]:
                if dsl and d not in dsl:
                    continue
                for it in lookup(conn, d, ids, ctx=ctx).items:
                    if it.found and it.evidence and (not kinds or it.evidence.unit_kind in kinds):
                        ev = it.evidence
                        ev.retrieval = Retrieval("exact", "exact", **base_ret, final_rank=len(exact) + 1)
                        ev.text, ev.text_truncated = ev.text[:TEXT_LIMIT_HIT], len(ev.text) > TEXT_LIMIT_HIT
                        exact.append(ev)
        if dss and not any(d in indexed for d in dss):
            resp.notes.append(f"document(s) {dss} named in the query are not indexed")

    # (2) retrievers
    match = sanitize_fts(query, operator="OR", drop_stop=True)
    lists: dict[str, list[int]] = {}
    bm: dict[int, tuple[float, int]] = {}
    vec: dict[int, dict[str, dict[str, float | int]]] = {}
    if mode in ("bm25", "hybrid"):
        r = coverage_gate(conn, query, bm25_ranked(conn, match, CANDIDATES, dsl, kinds))
        lists["fts"] = [u for u, _ in r]
        bm = {u: (s, i) for i, (u, s) in enumerate(r, start=1)}
    want_fig = include_figures and mode != "bm25" and (not kinds or "figure" in kinds)
    same_space = figure_model == model  # one model: text units and figures share ONE vector space
    qv: list[float] | None = None
    vec_pairs: list[tuple[int, float]] = []
    if mode in ("vector", "hybrid"):
        try:
            qv = embedder_factory(model).embed_texts([query], role="query")[0]
            vec_pairs = vector_ranked(conn, qv, model, CANDIDATES, dsl, kinds)
            for i, (u, d) in enumerate(vec_pairs, start=1):
                vec.setdefault(u, {})[model] = {"distance": round(d, 5), "similarity": round(1 - d, 5), "rank": i}
        except Exception as e:  # noqa: BLE001
            if mode == "vector":
                raise
            resp.notes.append(f"vector retrieval unavailable, BM25 only: {type(e).__name__}: {str(e)[:160]}")
    fig_pairs: list[tuple[int, float]] = []
    if want_fig:
        try:
            fq = (
                qv
                if (same_space and qv is not None)
                else embedder_factory(figure_model).embed_texts([query], role="query")[0]
            )
            fig_pairs = figure_ranked(conn, fq, figure_model, 10 if same_space else 3, dsl)
            for i, (u, d) in enumerate(fig_pairs, start=1):
                vec.setdefault(u, {})[f"{figure_model}:image"] = {
                    "distance": round(d, 5),
                    "similarity": round(1 - d, 5),
                    "rank": i,
                }
        except Exception as e:  # noqa: BLE001
            resp.notes.append(f"figure retrieval unavailable: {type(e).__name__}: {str(e)[:160]}")
    if same_space and fig_pairs:  # single model: merge text-chunk and figure distances into one list
        vec_pairs = sorted(vec_pairs + fig_pairs, key=lambda p: p[1])[:CANDIDATES]
        fig_pairs = []
    if vec_pairs:
        lists["vec"] = [u for u, _ in vec_pairs]
    if fig_pairs:
        lists["fig"] = [u for u, _ in fig_pairs]

    weights = {"fts": w_fts, "vec": w_vec, "fig": w_vec * 0.5}
    fused = weighted_rrf([(lst, weights[n]) for n, lst in lists.items()], C.RRF_K)
    ordered = sorted(fused, key=lambda u: -fused[u])
    exact_keys = {(e.doc_id, e.unit_id) for e in exact}
    rowmap: dict[int, sqlite3.Row] = {}
    if ordered:
        q = ",".join("?" * len(ordered))
        rowmap = {r["id"]: r for r in conn.execute(f"SELECT * FROM units WHERE id IN ({q})", ordered)}
    ordered = [u for u in ordered if (rowmap[u]["doc_id"], rowmap[u]["unit_id"]) not in exact_keys]
    resp.total_candidates = len(ordered) + len(exact)

    # (3) rerank the top candidates
    rr_scores: dict[int, float] = {}
    final_ranker = "wrrf" if mode == "hybrid" else ("bm25" if mode == "bm25" else f"vector:{model}")
    if rr_key and ordered:
        try:
            rr = reranker_factory(rr_key)
            if rr is not None:
                head = ordered[:n_cand]
                scores = rr.rerank(query, [_doc_for_rerank(rowmap[u], ctx) for u in head])
                rr_scores = dict(zip(head, scores, strict=True))
                head.sort(key=lambda u: -rr_scores[u])
                ordered = head + ordered[n_cand:]
                final_ranker += f"+rerank:{rr_key}"
        except Exception as e:  # noqa: BLE001
            resp.notes.append(f"reranker {rr_key} failed, keeping fused order: {type(e).__name__}: {str(e)[:160]}")
            err = str(e)[:200]
        else:
            err = None
    else:
        err = None

    # (4) paginate + build evidence
    out: list[Evidence] = []
    if offset < len(exact):
        out += exact[offset : offset + limit]
    room = limit - len(out)
    start = max(0, offset - len(exact))
    for i, uid in enumerate(ordered[start : start + max(0, room)], start=len(exact) + start + 1):
        u = rowmap[uid]
        ret = Retrieval(
            "search",
            final_ranker,
            **base_ret,
            final_rank=i,
            bm25=bm[uid][0] if uid in bm else None,
            bm25_rank=bm[uid][1] if uid in bm else None,
            vector=vec.get(uid, {}),
            rrf=round(fused[uid], 6),
            rerank={rr_key: round(rr_scores[uid], 5)} if uid in rr_scores and rr_key else {},
            rerank_error=err,
        )
        snip = _fts_snippet(conn, match, uid) or _plain_snippet(u["text"] or "", query)
        out.append(ctx.evidence(u, retrieval=ret, snippet=snip, text_limit=TEXT_LIMIT_HIT))
    resp.hits = out
    resp.has_more = offset + limit < resp.total_candidates
    return resp


def rank_of(hits: Sequence[Evidence], ds: str, unit_id: str) -> int | None:
    """1-based rank of (ds, unit_id) in a hit list, else None."""
    for i, h in enumerate(hits, start=1):
        if h.doc_id == ds and h.unit_id == unit_id:
            return i
    return None


# ============================================================================ documents + images
def list_documents(conn: sqlite3.Connection, filter: str | None = None) -> list[DocumentInfo]:  # noqa: A002
    """Indexed documents with edition and per-model embedding status; ``filter`` = substring of id/title."""
    out = []
    for r in conn.execute("SELECT * FROM documents ORDER BY doc_id"):
        if filter and filter.lower() not in f"{r['doc_id']} {r['title']}".lower():
            continue
        models = {
            f"{m['model_key']}/{m['kind']}": f"{m['status']} {m['n_ok']}/{m['n_expected']}"
            for m in conn.execute("SELECT * FROM document_models WHERE doc_id=?", (r["doc_id"],))
        }
        out.append(
            DocumentInfo(
                r["doc_id"],
                r["title"],
                r["edition"],
                r["edition_label"],
                r["pages"],
                r["n_units"],
                r["n_chunks"],
                r["n_figures"],
                r["source_pdf"],
                C.pdf_path(r["source_pdf"], r["source_path"]),
                r["source_sha256"],
                r["indexed_at"],
                r["source_item_id"],
                models,
            )
        )
    return out


def figure(conn: sqlite3.Connection, ds: str, figure_id: str, fig_dir: Path | None = None) -> ImageResult:
    """PNG crop of a figure plus its Evidence record."""
    fid = parse_ref(figure_id)
    if not fid.startswith("Figure"):
        fid = "Figure " + fid
    r = conn.execute("SELECT * FROM units WHERE doc_id=? AND unit_id=? AND kind='figure'", (ds, fid)).fetchone()
    if not r:
        known = [x[0] for x in conn.execute("SELECT unit_id FROM units WHERE doc_id=? AND kind='figure'", (ds,))]
        return ImageResult(
            False, ds, fid, message=f"no {fid} in {C.doc_label(ds)}; known figures: {', '.join(known) or 'none'}"
        )
    ctx = _Ctx(conn, fig_dir=fig_dir)
    ev = ctx.evidence(r, retrieval=Retrieval("figure", "exact"))
    return ImageResult(
        ev.figure_png is not None,
        ds,
        fid,
        ev.figure_png,
        (json.loads(r["extra"] or "{}")).get("caption", ""),
        r["page_start"],
        ev,
        "" if ev.figure_png else ev.null_reasons.get("figure_png", "image file missing"),
    )


def page_image(
    conn: sqlite3.Connection,
    ds: str,
    page: int,
    dpi: int = 110,
    pdf_dir: Path | None = None,
    fig_dir: Path | None = None,
) -> ImageResult:
    """PNG of a whole PDF page rendered on demand from the source PDF (cached with the sheet)."""
    fig_dir = fig_dir or C.FIG_DIR
    s = conn.execute("SELECT * FROM documents WHERE doc_id=?", (ds,)).fetchone()
    if not s:
        return ImageResult(False, ds, f"page {page}", message=f"{C.doc_label(ds)} is not indexed")
    if not 1 <= page <= s["pages"]:
        return ImageResult(False, ds, f"page {page}", message=f"{C.doc_label(ds)} has {s['pages']} pages")
    out_dir = fig_dir / f"{ds}_{edition_tag(s['edition'])}"
    out = out_dir / f"page_{page:03d}_{dpi}dpi.png"
    if not out.exists():
        pdf = (
            Path(s["source_path"])
            if s["source_path"] and Path(s["source_path"]).exists()
            else (pdf_dir or C.DEFAULT_PDF_DIR) / s["source_pdf"]
        )
        if not pdf.exists():
            return ImageResult(False, ds, f"page {page}", message=f"source PDF {s['source_pdf']} not found")
        import pypdfium2 as pdfium

        out_dir.mkdir(parents=True, exist_ok=True)
        pdfium.PdfDocument(str(pdf))[page - 1].render(scale=dpi / 72).to_pil().save(out, optimize=True)
    return ImageResult(True, ds, f"page {page}", str(out), page=page)


get_figure = figure
get_page_image = page_image
