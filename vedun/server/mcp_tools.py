"""The ten MCP tools of the vedun knowledge base (spec 2026-07-28, official Python SDK ``mcp`` 2.x ``MCPServer``).

One tool per agent intent, all read-only wrappers over :mod:`vedun.search` / :mod:`vedun.documents` /
:mod:`vedun.quote` / :mod:`vedun.check_citations`; nothing of the retrieval is re-implemented here.

* every Evidence record keeps its full provenance (``Evidence`` + three host paths);
* structured output (``outputSchema`` + ``structuredContent``) plus a human-readable text block, both self-sufficient
  (a client may show the model either of them); the two image tools return content blocks only, because some clients
  drop image blocks when ``structuredContent`` is present;
* every call is capped (``MAX_TEXT_CHARS`` for the text block, ``MAX_JSON_CHARS`` for ``structuredContent``) and
  continues with an opaque ``next_cursor`` that is bound to the tool and to its arguments;
* inputs are validated by the JSON schema before the function runs (document number regex, lengths, enums); a malformed
  FTS5 expression, a bad cursor or an unknown document becomes a tool error (``isError``), never an exception;
* the database is opened read-only per call (``settings.open_readonly``), so a running ``sync`` is never blocked;
* ``kb_search`` is the only tool that can spend money; it obeys the daily budget exactly like ``GET /api/search``.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from vedun import catalog as K
from vedun import config as C
from vedun import documents as docs
from vedun import search as kb
from vedun.check_citations import check_citations
from vedun.evidence import Evidence
from vedun.quote import verify_quote

from . import api, audit, budget
from . import settings as S

_TOOL_REF = re.compile(
    r"\bkb_(lookup|toc|fulltext|search|get_document|figure|page_image|verify_quote|documents|check_citations)\b"
)


def localize(text: str) -> str:
    """Tool names in ``text`` with the profile's prefix (code and descriptions are written with the default ``kb_``)."""
    return _TOOL_REF.sub(lambda m: f"{C.MCP_TOOL_PREFIX}_{m[1]}", text)


# ============================================================================ limits (read at call time: tests patch them)
MAX_TEXT_CHARS = 20_000  # human-readable block per call
MAX_JSON_CHARS = 40_000  # structuredContent per call (Claude Code: warns >10k tokens, spills text >50k chars)
MIN_WINDOW_CHARS = 1000  # smallest window of an oversized unit text per page
MAX_DOC_PAGE_CHARS = 20_000  # kb_get_document md page
DEFAULT_DOC_PAGE_CHARS = 15_000
MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGE_SIDE = 1600
MAX_CITATION_TEXT = 50_000
MAX_QUOTE = 3_000
CURSOR_VERSION = 1

# ============================================================================ argument types
DocIdArg = Annotated[str, Field(pattern=S.SHEET_RE, description='Document id as cited, e.g. "4-12" or "11-7".')]
CursorArg = Annotated[
    str | None,
    Field(
        max_length=600,
        description="Opaque continuation token: pass the previous result's next_cursor with the SAME other arguments.",
    ),
]
UnitKind = Literal["clause", "table", "figure", "appendix"]
DocIdFilter = (
    Annotated[str, Field(pattern=S.SHEET_RE)]
    | Annotated[list[Annotated[str, Field(pattern=S.SHEET_RE)]], Field(max_length=20)]
    | None
)
KindFilter = UnitKind | Annotated[list[UnitKind], Field(max_length=4)] | None


# ============================================================================ output models
@dataclass
class EvidenceOut(Evidence):
    """``Evidence`` plus the host paths (Windows paths Claude Code can open directly) and the text window."""

    source_pdf_host: str | None = None
    md_path_host: str | None = None
    figure_png_host: str | None = None
    page_png_host: str | None = None
    text_offset: int = 0  # start of ``text`` inside the full unit text (non-zero on continuation pages)


class LookupItemOut(BaseModel):
    requested: str = Field(description="The reference as the caller wrote it.")
    found: bool
    continued: bool = Field(default=False, description="True when this item started on an earlier page.")
    evidence: EvidenceOut | None = None
    duplicates: list[EvidenceOut] = Field(
        default_factory=list, description="The source repeats the id: the other copies."
    )
    children: list[EvidenceOut] = Field(default_factory=list)
    prev_id: str | None = None
    next_id: str | None = None
    nearest: list[str] = Field(default_factory=list)
    possibly_renumbered_to: list[str] = Field(default_factory=list)
    message: str = ""


class LookupOut(BaseModel):
    doc_id: str
    document_indexed: bool
    all_found: bool
    items: list[LookupItemOut]
    message: str = ""
    next_cursor: str | None = None


class TocItemOut(BaseModel):
    unit_id: str
    kind: str
    title: str
    depth: int
    n_children: int
    parent: str | None


class TocOut(BaseModel):
    doc_id: str
    document_title: str | None
    edition: str | None
    edition_label: str | None
    prefix: str | None
    items: list[TocItemOut]
    total: int
    offset: int
    next_cursor: str | None = None


class FulltextOut(BaseModel):
    query: str
    syntax: str
    match_expression: str
    hits: list[EvidenceOut]
    total: int = Field(description="Matching units in the whole index.")
    offset: int
    next_cursor: str | None = None


class SearchOut(BaseModel):
    query: str
    mode: str
    fusion: str
    w_fts: float
    w_vec: float
    reranker: str | None
    reranked: bool
    rerank_error: str | None
    budget_exhausted: bool
    degraded: list[str] = Field(default_factory=list, description="Everything that reduced quality: read it.")
    hits: list[EvidenceOut]
    total_candidates: int
    offset: int
    next_cursor: str | None = None


class DocPaths(BaseModel):
    pdf_exists: bool = False
    pdf_path_host: str | None = None
    pdf_path_container: str | None = None
    md_exists: bool = False
    md_path_host: str | None = None
    md_path_container: str | None = None


class DocTocItem(BaseModel):
    unit_id: str
    kind: str
    title: str
    n_children: int


class DocumentOut(BaseModel):
    found: bool
    doc_id: str
    format: str
    indexed: bool = True
    title: str | None = None
    edition: str | None = None
    edition_label: str | None = None
    citation_prefix: str | None = None
    pages: int | None = None
    units: dict[str, int] = Field(default_factory=dict)
    toc: list[DocTocItem] = Field(default_factory=list, description="Top-level structure (fmt meta / pdf).")
    paths: DocPaths = Field(default_factory=DocPaths)
    url: str | None = None
    section: str | None = None
    text: str | None = Field(default=None, description="fmt md: the Markdown page.")
    offset: int = 0
    returned_chars: int = 0
    total_chars: int = 0
    message: str = ""
    nearest: list[str] = Field(default_factory=list)
    next_cursor: str | None = None


class VerifyOut(BaseModel):
    verdict: Literal["OK", "NO MATCH"]
    match: bool
    in_unit: bool
    in_pdf: bool | None
    requested_ref: str
    message: str
    pdf_message: str = ""
    found_in_units: list[str] = Field(default_factory=list)
    found_in_citations_pl: list[str] = Field(default_factory=list)
    closest: str | None = None
    closest_ratio: float = 0.0
    closest_unit_id: str | None = None
    closest_citation_pl: str | None = None
    evidence: EvidenceOut | None = None


class DocumentRow(BaseModel):
    doc_id: str
    title: str | None = None
    indexed: bool
    edition: str | None = None
    edition_label: str | None = None
    catalog_status: str
    latest_edition: str | None = None
    local_pdf: str | None = None
    index_stale: bool = False
    pages: int | None = None
    n_units: int | None = None
    n_figures: int | None = None


class DocumentsOut(BaseModel):
    documents: list[DocumentRow]
    total: int
    offset: int
    counts: dict[str, int]
    catalog_fetched_at: str | None = None
    next_cursor: str | None = None


class CitationRow(BaseModel):
    line: int
    citation: str
    doc_id: str
    unit_id: str
    status: Literal["OK", "UNKNOWN UNIT", "DOCUMENT NOT INDEXED"]
    suggestion: str = ""


class CitationsOut(BaseModel):
    all_ok: bool
    total: int
    offset: int
    counts: dict[str, int]
    results: list[CitationRow]
    next_cursor: str | None = None


# ============================================================================ plumbing
def _fail(message: str) -> NoReturn:
    raise ToolError(message)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    try:
        conn = S.open_readonly()
    except sqlite3.Error as e:  # no path in the message: it goes to the model
        raise ToolError(f"knowledge base database unavailable ({type(e).__name__})") from e
    try:
        yield conn
    finally:
        conn.close()


def _indexed(conn: sqlite3.Connection) -> list[str]:
    return [r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")]


def _args_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _encode_cursor(tool: str, key: str, offset: int, char: int = 0) -> str:
    raw = json.dumps({"v": CURSOR_VERSION, "t": tool, "k": key, "o": offset, "c": char}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None, tool: str, key: str) -> tuple[int, int]:
    """(offset, char offset) of a cursor; a foreign, damaged or stale cursor is a tool error the model can fix."""
    if cursor is None:
        return 0, 0
    try:
        d = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        ok = (
            d["v"] == CURSOR_VERSION
            and d["t"] == tool
            and isinstance(d["k"], str)
            and type(d["o"]) is int
            and type(d["c"]) is int
            and 0 <= d["o"] <= 10**9
            and 0 <= d["c"] <= 10**9
        )
    except (ValueError, KeyError, TypeError):
        ok = False
        d = {}
    if not ok:
        _fail(
            "invalid cursor: use the next_cursor of a previous result of this same tool, or omit cursor to start again"
        )
    if d["k"] != key:
        _fail(
            "this cursor belongs to a different query: pass the same arguments as in the call that returned it, "
            "or omit cursor to start again"
        )
    return d["o"], d["c"]


def _fit(parts: Sequence[tuple[int, int]]) -> int:
    """How many consecutive items fit one page; ``parts`` = (text chars, json chars) per item. At least one."""
    text = js = 0
    for i, (t, j) in enumerate(parts):
        if i and (text + t > MAX_TEXT_CHARS or js + j > MAX_JSON_CHARS):
            return i
        text, js = text + t, js + j
    return len(parts)


def _jlen(o: Any) -> int:
    return len(json.dumps(o, ensure_ascii=False, default=str))


def _host_fig(p: str | None) -> str | None:
    """Figure/page PNG path as the Windows host sees it (``VEDUN_HOST_ROOT``/figures), else the local path."""
    if not p:
        return None
    root = C.env("HOST_ROOT")
    if root:
        try:
            return root.replace("\\", "/").rstrip("/") + "/figures/" + Path(p).relative_to(C.FIG_DIR).as_posix()
        except ValueError:
            pass
    return str(p).replace("\\", "/")


def _ev(ev: Evidence, *, text_from: int = 0, text_to: int | None = None) -> dict[str, Any]:
    """Evidence -> plain dict with host paths; ``text_from/to`` select a window of the verbatim text."""
    d = asdict(ev)
    d["source_pdf_host"] = docs.host_path(ev.source_pdf, "pdf") if ev.source_pdf else None
    d["md_path_host"] = docs.host_path(ev.md_path, "md") if ev.md_path else None
    d["figure_png_host"] = _host_fig(ev.figure_png)
    d["page_png_host"] = _host_fig(ev.page_png)
    d["text_offset"] = text_from
    if text_from or text_to is not None:
        full = ev.text
        d["text"] = full[text_from:text_to]
        d["text_truncated"] = bool(ev.text_truncated or (text_to is not None and text_to < len(full)) or text_from)
    return d


def _ev_model(ev: Evidence, d: dict[str, Any] | None = None) -> EvidenceOut:
    """The ``EvidenceOut`` of an Evidence (``d`` = its already built dict, with a text window if any)."""
    d = d if d is not None else _ev(ev)
    return EvidenceOut(
        **{f.name: d[f.name] for f in fields(EvidenceOut) if f.name != "retrieval"}, retrieval=ev.retrieval
    )


def _where(e: dict[str, Any]) -> str:
    return " > ".join(f"{p['unit_id']} {p['title']}".strip() for p in e["parent_path"])


def _fmt_ev(e: dict[str, Any], n: int | None = None, *, text: bool = True, snippet: bool = False) -> str:
    """Human-readable block of one Evidence dict (citation first, then location, files, verbatim text)."""
    lines = [f"{f'[{n}] ' if n else ''}{e['citation_pl']}", f"    EN: {e['citation_en']}"]
    kind = f"{e['unit_id']} [{e['unit_kind']}]" + (f" | {e['title']}" if e["title"] else "")
    lines.append(f"    {kind}")
    if e["parent_path"]:
        lines.append(f"    in: {_where(e)}")
    r = e["retrieval"]
    if r.get("final_ranker") not in (None, "exact") or r.get("match_type") in ("search", "exact"):
        sc = []
        for k in ("bm25", "rrf"):
            if r.get(k) is not None:
                sc.append(f"{k}={r[k]:.3f}")
        for k, v in (r.get("rerank") or {}).items():
            sc.append(f"rerank:{k}={v:.3f}")
        lines.append(f"    found by: {r.get('match_type')}/{r.get('final_ranker')} {' '.join(sc)}".rstrip())
    files = f"    files: PDF {e['source_pdf_host']}"
    if e["md_path_host"]:
        files += f" | MD {e['md_path_host']} lines {e['md_line_start']}-{e['md_line_end']}"
    if e["figure_png_host"]:
        files += f" | PNG {e['figure_png_host']}"
    lines.append(files)
    if snippet and e.get("snippet") and e["text_truncated"]:
        lines.append(f"    snippet: {e['snippet']}")
    if text:
        total = e["text_chars"]
        shown = len(e["text"])
        note = ""
        if e["text_truncated"]:
            note = localize(
                f" (chars {e['text_offset']}-{e['text_offset'] + shown} of {total}; the rest: kb_lookup / cursor)"
            )
        lines.append(f"    --- verbatim text{note} ---")
        lines.append(e["text"])
        lines.append("    --- end ---")
    return "\n".join(lines)


def _ok(text: str, data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def _audited(tool: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """One audit line per call: tool name and outcome. Never the arguments (query text stays out of the log)."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*a: Any, **k: Any) -> Any:
            outcome = "error"
            try:
                r = fn(*a, **k)
                outcome = "ok"
                return r
            finally:
                audit.log_event("mcp_tool", outcome=outcome, detail=tool)

        return wrapper

    return deco


RO = dict(read_only_hint=True, destructive_hint=False)


# ============================================================================ lookup
@dataclass
class _Entry:
    item: int
    role: str  # main | dup | child | none
    ev: Evidence | None


def _flatten(items: Sequence[kb.LookupItem]) -> list[_Entry]:
    out: list[_Entry] = []
    for i, it in enumerate(items):
        if it.found and it.evidence is not None:
            out.append(_Entry(i, "main", it.evidence))
            out += [_Entry(i, "dup", d) for d in it.duplicates]
            out += [_Entry(i, "child", c) for c in it.children]
        else:
            out.append(_Entry(i, "none", None))
    return out


def _cut_point(text: str, start: int, budget: int) -> int:
    """End index of a window of ``text`` starting at ``start`` of at most ``budget`` chars, on a line/word edge."""
    end = min(len(text), start + budget)
    if end >= len(text):
        return end
    for sep in ("\n\n", "\n", " "):
        p = text.rfind(sep, start + budget // 2, end)
        if p != -1:
            return p + len(sep)
    return end


LOOKUP_DESC = (
    "Exact lookup of ONE KNOWN clause, table, figure or appendix of a document; returns its verbatim text "
    'with full provenance. Use when document and unit are already known: doc_id="4-12", ref="Section 2.3.1" (also "2.3.1", '
    '"pkt 2.3.1", "Table 2.1.1", "Tab. 2.1.1", "Fig. 2.1.1", "rys. 2.1.1", "Appendix A"). Several units of one document: '
    "refs=[...] (max 20). Deterministic: no fuzzy matching, no network, no cost. An unknown id is a normal result "
    "(found=false) listing the nearest ids and, after a document revision, 'possibly renumbered to' candidates. "
    "include_children adds every unit under a clause; neighbors adds previous/next sibling ids. Large results are "
    "paged (next_cursor). If the unit is not known use kb_search (descriptive question) or kb_fulltext (exact words, "
    "numbers); to read a whole chapter use kb_get_document. Cite with the returned citation_pl / citation_en exactly as "
    "given (edition included, no page number): a clause number remembered from an older note is not a citation. "
    "Before a quotation goes into a deliverable, confirm it with kb_verify_quote."
)


def kb_lookup(
    doc_id: DocIdArg,
    ref: Annotated[
        str | None, Field(min_length=1, max_length=80, description='One reference, e.g. "Section 2.3.1".')
    ] = None,
    refs: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=80)]] | None,
        Field(max_length=20, description="Several references of the same document (max 20)."),
    ] = None,
    include_children: Annotated[bool, Field(description="Also return everything under the clause.")] = False,
    neighbors: Annotated[bool, Field(description="Also return the previous/next sibling ids.")] = False,
    cursor: CursorArg = None,
) -> Annotated[CallToolResult, LookupOut]:
    wanted = list(dict.fromkeys([*([ref] if ref else []), *(refs or [])]))
    if not wanted:
        _fail('give "ref" (one reference) or "refs" (a list), e.g. ref="Section 2.3.1"')
    if len(wanted) > 20:
        _fail("at most 20 references per call")
    key = _args_key(doc_id, wanted, include_children, neighbors)
    pos, coff = _decode_cursor(cursor, "kb_lookup", key)
    with _db() as conn:
        resp = kb.lookup(conn, doc_id, wanted, include_children=include_children, neighbors=neighbors)
        indexed = _indexed(conn) if not resp.document_indexed else []
    entries = _flatten(resp.items)
    if pos >= len(entries):
        _fail("cursor is beyond the end of this result: omit cursor to start again")

    # --- the entries of this page; the text of an oversized unit is windowed across pages
    window: list[tuple[_Entry, int, int]] = []  # entry, text start, text end
    used_t = used_j = 0
    i, next_pos, next_char = pos, None, 0
    while i < len(entries):
        e = entries[i]
        start = coff if i == pos else 0
        full = e.ev.text if e.ev else ""
        meta_j = _jlen(_ev(e.ev, text_from=len(full))) if e.ev else 100
        budget = max(0, min(MAX_TEXT_CHARS - used_t - 700, int((MAX_JSON_CHARS - used_j - meta_j) / 1.1)))
        n_left = len(full) - start
        if n_left <= budget:
            window.append((e, start, len(full)))
            used_t += n_left + 700
            used_j += meta_j + int(n_left * 1.1)
            i += 1
            continue
        if window:  # does not fit next to what is already here: the next page starts with it
            next_pos, next_char = i, start
            break
        end = _cut_point(full, start, max(MIN_WINDOW_CHARS, budget))  # alone and too big: a window of it
        window.append((e, start, end))
        if end >= len(full):
            i += 1
        else:
            next_pos, next_char = i, end
        break
    if next_pos is None and i < len(entries):
        next_pos = i
    cursor_out = _encode_cursor("kb_lookup", key, next_pos, next_char) if next_pos is not None else None

    # --- rebuild the items from the entries on this page
    out_items: dict[int, LookupItemOut] = {}
    blocks: list[str] = []
    for e, a, b in window:
        it = resp.items[e.item]
        o = out_items.get(e.item)
        if o is None:
            o = out_items[e.item] = LookupItemOut(
                requested=it.requested,
                found=it.found,
                continued=e.role in ("dup", "child") or (e.role == "main" and a > 0),
                prev_id=it.prev_id,
                next_id=it.next_id,
                nearest=it.nearest,
                possibly_renumbered_to=it.possibly_renumbered_to,
                message=it.message,
            )
        if e.ev is None:
            continue
        d = _ev(e.ev, text_from=a, text_to=b) if (a or b < len(e.ev.text)) else _ev(e.ev)
        model = _ev_model(e.ev, d)
        if e.role == "main":
            o.evidence = model
        elif e.role == "dup":
            o.duplicates.append(model)
        else:
            o.children.append(model)
        label = {"main": "", "dup": "(duplicate id in the source) ", "child": "(child) "}[e.role]
        blocks.append(label + _fmt_ev(d))
    lines = [C.doc_label(doc_id) + " lookup: " + ", ".join(f'"{w}"' for w in wanted)]
    if not resp.document_indexed:
        lines.append(
            f"NOT FOUND: {resp.message or f'{C.doc_label(doc_id)} is not indexed'}. Indexed documents: {', '.join(indexed) or 'none'}."
        )
    for idx in sorted(out_items):
        o = out_items[idx]
        if not o.found:
            near = f" Nearest ids: {', '.join(o.nearest)}." if o.nearest else ""
            ren = f" Possibly renumbered to: {', '.join(o.possibly_renumbered_to)}." if o.possibly_renumbered_to else ""
            lines.append(f'NOT FOUND "{o.requested}": {o.message}{near}{ren}')
        elif o.prev_id or o.next_id:
            lines.append(f'"{o.requested}": previous {o.prev_id or "-"}, next {o.next_id or "-"}')
    lines += blocks
    if cursor_out:
        lines.append(f"[more: call again with cursor={cursor_out}]")
    data = LookupOut(
        doc_id=doc_id,
        document_indexed=resp.document_indexed,
        all_found=resp.found,
        items=[out_items[k] for k in sorted(out_items)],
        message=resp.message,
        next_cursor=cursor_out,
    ).model_dump(mode="json")
    return _ok("\n".join(lines), data)


# ============================================================================ toc
TOC_DESC = (
    "Browse the STRUCTURE of a document or of one branch of it: unit ids, kinds, headings and child counts, no clause "
    'text (cheap). Use to discover which clauses exist (e.g. everything under prefix "2.4.1") before kb_lookup, or to '
    "see how a document is organised. depth is relative to prefix (1 = direct children only; omit for the whole branch). "
    "An unknown document or prefix is an error that lists the nearest ids. Paged with next_cursor."
)


def kb_toc(
    doc_id: DocIdArg,
    prefix: Annotated[
        str | None, Field(max_length=60, description='Clause id of the branch, e.g. "2.4.1". Omit = whole document.')
    ] = None,
    depth: Annotated[int | None, Field(ge=1, le=12, description="Levels below prefix.")] = None,
    limit: Annotated[int, Field(ge=1, le=300, description="Max items per page (also capped by size).")] = 100,
    cursor: CursorArg = None,
) -> Annotated[CallToolResult, TocOut]:
    key = _args_key(doc_id, prefix, depth)
    offset, _ = _decode_cursor(cursor, "kb_toc", key)
    with _db() as conn:
        r = kb.toc(conn, doc_id, prefix, depth, limit, offset)
        indexed = _indexed(conn)
    if not r.found:
        _fail(f"{r.message or f'{C.doc_label(doc_id)} is not indexed'}. Indexed documents: {', '.join(indexed)}.")
    if offset and offset >= r.total:
        _fail("cursor is beyond the end of this result: omit cursor to start again")
    rows = [asdict(i) for i in r.items]
    lines_ = [
        f"{'  ' * i['depth']}{i['unit_id']}  [{i['kind']}]  {i['title']}  ({i['n_children']} children)" for i in rows
    ]
    k = _fit([(len(s) + 1, _jlen(x)) for s, x in zip(lines_, rows, strict=True)])
    nxt = offset + k if (k < len(rows) or r.has_more) else None
    cur = _encode_cursor("kb_toc", key, nxt) if nxt is not None else None
    cite = f"{C.doc_label(doc_id)} „{r.document_title}” ({r.edition_label or r.edition})"
    head = f"{cite} structure{f' under {r.prefix}' if r.prefix else ''}: {r.total} units, showing {offset + 1}-{offset + k}"
    text = head + "\n" + "\n".join(lines_[:k]) + (f"\n[more: call again with cursor={cur}]" if cur else "")
    data = TocOut(
        doc_id=doc_id,
        document_title=r.document_title,
        edition=r.edition,
        edition_label=r.edition_label,
        prefix=r.prefix,
        items=[TocItemOut(**{k_: x[k_] for k_ in TocItemOut.model_fields}) for x in rows[:k]],
        total=r.total,
        offset=offset,
        next_cursor=cur,
    ).model_dump(mode="json")
    return _ok(text, data)


# ============================================================================ fulltext
FULLTEXT_DESC = (
    "Keyword search (SQLite FTS5 / BM25) over all indexed documents: no embeddings, no reranker, no paid API call, "
    'deterministic. Use for an exact phrase, a number with its unit ("25 mm"), an identifier, or boolean/proximity '
    "logic. syntax='safe' (default): terms are AND-ed, a double-quoted phrase stays a phrase. syntax='fts5': "
    '"exact phrase", prefix*, AND / OR / NOT (capital letters), NEAR(a b, 5); a malformed expression returns an error '
    "with the parser message. The corpus is English and stemmed (cylinders ~ cylinder) while Polish words do not match: "
    "for a descriptive or Polish question use kb_search. Hit text is capped at 1500 characters (text_truncated): get "
    "the full unit with kb_lookup. Filter with doc_id and kind. Ranked by BM25; next_cursor pages. Cite via citation_pl."
)


def kb_fulltext(
    query: Annotated[
        str, Field(min_length=1, max_length=500, description='Words or an FTS5 expression, e.g. "25 mm".')
    ],
    doc_id: Annotated[
        DocIdFilter, Field(description='Restrict to document(s), e.g. "4-12" or ["4-12","11-7"].')
    ] = None,
    kind: Annotated[KindFilter, Field(description="Restrict to unit kind(s).")] = None,
    syntax: Annotated[
        Literal["safe", "fts5"], Field(description="safe = AND-ed terms; fts5 = full FTS5 syntax.")
    ] = "safe",
    limit: Annotated[int, Field(ge=1, le=30, description="Hits per page (also capped by size).")] = 10,
    cursor: CursorArg = None,
) -> Annotated[CallToolResult, FulltextOut]:
    key = _args_key(query, doc_id, kind, syntax)
    offset, _ = _decode_cursor(cursor, "kb_fulltext", key)
    with _db() as conn:
        r = kb.fulltext(conn, query, doc_id, kind, limit, offset, syntax)
    if not r.ok:
        hint = (
            ' Hint: in syntax "fts5" quote phrases ("a b"), capitalise AND/OR/NOT, write NEAR(a b, 5); '
            'syntax "safe" never fails on punctuation.'
            if syntax == "fts5"
            else ""
        )
        _fail(f"{r.error or 'invalid query'}.{hint}")
    return _hits_result(
        "kb_fulltext",
        key,
        offset,
        r.hits,
        r.total,
        r.has_more,
        query,
        head_extra=f"syntax={syntax}",
        build=lambda hits, cur: FulltextOut(
            query=query,
            syntax=syntax,
            match_expression=r.match_expression,
            hits=hits,
            total=r.total,
            offset=offset,
            next_cursor=cur,
        ),
    )


def _hits_result(
    tool: str,
    key: str,
    offset: int,
    hits: Sequence[Evidence],
    total: int,
    has_more: bool,
    query: str,
    *,
    head_extra: str,
    build: Callable[[list[EvidenceOut], str | None], BaseModel],
    notes: Sequence[str] = (),
) -> CallToolResult:
    """Common tail of kb_fulltext / kb_search: fit the hits to the page budget, cursor, text block, structured data."""
    dicts = [_ev(h) for h in hits]
    blocks = [_fmt_ev(d, offset + i + 1, snippet=True) for i, d in enumerate(dicts)]
    k = _fit([(len(b) + 2, _jlen(d)) for b, d in zip(blocks, dicts, strict=True)]) if dicts else 0
    nxt = offset + k if k and (k < len(dicts) or has_more) else None
    cur = _encode_cursor(tool, key, nxt) if nxt is not None else None
    models = [_ev_model(h, d) for d, h in zip(dicts[:k], hits[:k], strict=True)]
    shown = f"showing {offset + 1}-{offset + k}" if k else "no hits"
    head = f'{tool} "{query}" ({head_extra}): {total} matching units, {shown}'
    parts = [head, *[f"NOTE: {n}" for n in notes], *blocks[:k]]
    if cur:
        parts.append(f"[more: call again with cursor={cur}]")
    return _ok("\n\n".join(parts), build(models, cur).model_dump(mode="json"))


# ============================================================================ search
SEARCH_DESC = (
    "Semantic + keyword search for a DESCRIPTIVE question when the document or clause is not known (Polish or English). "
    "Weighted RRF of BM25 and embedding retrieval (defaults w_fts 0.4 / w_vec 0.6), figure hits included, then the Jev "
    "reranker on the top 30. CALLS EXTERNAL APIS (embeddings, reranker) and spends a daily budget; when the budget is "
    "used up, or an API fails, it answers from BM25 only and says so in budget_exhausted / rerank_error / degraded "
    "(read them: the ranking is weaker). Each page re-runs the retrieval. A hit is a candidate, not an answer: read its "
    "verbatim text, check the edition, cite via citation_pl. When the document and clause are known use kb_lookup; for an "
    "exact phrase or number use kb_fulltext (free); to read a chapter use kb_get_document. Confirm quotations with "
    "kb_verify_quote."
)


def kb_search(
    query: Annotated[str, Field(min_length=1, max_length=500, description="Question or topic, PL or EN.")],
    doc_id: Annotated[DocIdFilter, Field(description="Restrict to document(s).")] = None,
    kind: Annotated[KindFilter, Field(description="Restrict to unit kind(s).")] = None,
    w_fts: Annotated[float | None, Field(ge=0, le=1, description="BM25 weight (default 0.4).")] = None,
    w_vec: Annotated[float | None, Field(ge=0, le=1, description="Vector weight (default 0.6).")] = None,
    rerank: Annotated[
        Literal["default", "none", "r1", "r2", "r3"], Field(description="default = Jev (r3); none = fused order only.")
    ] = "default",
    include_figures: Annotated[bool, Field(description="Merge figure hits (image embeddings).")] = True,
    limit: Annotated[int, Field(ge=1, le=20, description="Hits per page (also capped by size).")] = 8,
    cursor: CursorArg = None,
) -> Annotated[CallToolResult, SearchOut]:
    key = _args_key(query, doc_id, kind, w_fts, w_vec, rerank, include_figures)
    offset, _ = _decode_cursor(cursor, "kb_search", key)
    mode: Literal["hybrid", "bm25", "vector"] = "hybrid"
    over_budget = budget.exhausted()
    if over_budget:  # no embedding, no rerank: BM25 only, and say so
        mode, rerank, include_figures = "bm25", "none", False
    with _db() as conn:
        try:
            r = kb.search(
                conn,
                query,
                doc_id,
                kind,
                w_fts=w_fts,
                w_vec=w_vec,
                rerank=None if rerank == "none" else rerank,
                include_figures=include_figures,
                limit=limit,
                offset=offset,
                mode=mode,
                embedder_factory=lambda k: api.hooks.embedder_factory(k),
                reranker_factory=lambda k: api.hooks.reranker_factory(k),
            )
        except Exception as e:  # noqa: BLE001 - a retrieval backend failure is reported, not raised
            _fail(
                localize(f"retrieval failed ({type(e).__name__}: {str(e)[:200]}). Try kb_fulltext, which needs no API.")
            )
    budget.flush()
    notes = list(r.notes)
    if over_budget:
        notes.append(
            f"{budget.EXHAUSTED_MESSAGE} ({S.daily_budget_usd():.2f} USD): BM25 only, no embeddings, no reranking"
        )
    err = next((h.retrieval.rerank_error for h in r.hits if h.retrieval.rerank_error), None)
    err = err or next((n for n in r.notes if n.startswith("reranker")), None)
    reranked = bool(r.reranker) and err is None and any(h.retrieval.rerank for h in r.hits)
    degraded = [*notes, *([f"rerank_error: {err}"] if err and not any(err in n for n in notes) else [])]
    return _hits_result(
        "kb_search",
        key,
        offset,
        r.hits,
        r.total_candidates,
        r.has_more,
        query,
        head_extra=f"mode={r.mode}, reranker={r.reranker if reranked else 'none'}",
        notes=degraded,
        build=lambda hits, cur: SearchOut(
            query=query,
            mode=r.mode,
            fusion=r.fusion,
            w_fts=r.w_fts,
            w_vec=r.w_vec,
            reranker=r.reranker,
            reranked=reranked,
            rerank_error=err,
            budget_exhausted=over_budget,
            degraded=degraded,
            hits=hits,
            total_candidates=r.total_candidates,
            offset=offset,
            next_cursor=cur,
        ),
    )


# ============================================================================ document
DOCUMENT_DESC = (
    "Whole-document or whole-chapter access (use when one clause is not enough; for known units use kb_lookup). "
    "fmt='meta' (default): title, edition, unit counts, top-level structure and the HOST paths (Windows) of the original "
    "PDF and of the converted Markdown, which can be opened with the file Read tool. fmt='md': the document's Markdown "
    "text, optionally one clause subtree (section='2.3'), paged: max_chars (default 15000, max 20000), next_cursor "
    "continues exactly where the page stopped, concatenated pages equal the full text. fmt='pdf': path and URL only "
    "(PDF bytes are never returned). Markdown is the converted text: quote only after kb_verify_quote."
)


def kb_get_document(
    doc_id: DocIdArg,
    fmt: Annotated[Literal["meta", "md", "pdf"], Field(description="meta | md | pdf")] = "meta",
    section: Annotated[
        str | None,
        Field(
            max_length=60,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9. \-]{0,59}$",
            description='md only: clause id, e.g. "2.3".',
        ),
    ] = None,
    max_chars: Annotated[
        int, Field(ge=1000, le=20_000, description="md only: page size in characters.")
    ] = DEFAULT_DOC_PAGE_CHARS,
    cursor: CursorArg = None,
) -> Annotated[CallToolResult, DocumentOut]:
    key = _args_key(doc_id, fmt, section)
    offset, _ = _decode_cursor(cursor, "kb_get_document", key)
    page = min(max_chars, MAX_DOC_PAGE_CHARS)
    with _db() as conn:
        try:
            d = docs.get_document(conn, doc_id, fmt, section, offset, page)
        except ValueError as e:
            _fail(str(e))
        indexed = _indexed(conn)
    if not d.get("found"):
        near = f" Nearest ids: {', '.join(d['nearest'])}." if d.get("nearest") else ""
        _fail(
            f"{d.get('message') or f'{C.doc_label(doc_id)} not found'}{near} Indexed documents: {', '.join(indexed)}."
        )
    paths = d.get("paths") or {}
    nxt = d.get("next_offset")
    cur = _encode_cursor("kb_get_document", key, nxt) if nxt is not None else None
    out = DocumentOut(
        found=True,
        doc_id=doc_id,
        format=fmt,
        indexed=d.get("indexed", True),
        title=d.get("title"),
        edition=d.get("edition"),
        edition_label=d.get("edition_label"),
        citation_prefix=d.get("citation_prefix"),
        pages=d.get("pages"),
        units=d.get("units") or {},
        toc=[DocTocItem(**t) for t in d.get("toc", [])],
        paths=DocPaths(**{k: v for k, v in paths.items() if k in DocPaths.model_fields}),
        url=d.get("url"),
        section=d.get("section"),
        text=d.get("text"),
        offset=d.get("offset", 0),
        returned_chars=d.get("returned_chars", 0),
        total_chars=d.get("total_chars", 0),
        message=d.get("message") or d.get("note") or "",
        next_cursor=cur,
    )
    pdf = paths.get("pdf_path_host")
    md = paths.get("md_path_host")
    head = f"{d.get('citation_prefix') or C.doc_label(doc_id)} ({fmt})"
    if fmt == "md":
        body = (
            f"{head}{f' section {out.section}' if out.section else ''}: chars {out.offset}-{out.offset + out.returned_chars} "
            f"of {out.total_chars}\n--- markdown ---\n{out.text}\n--- end ---"
        )
        if cur:
            body += f"\n[more: call again with cursor={cur}]"
    else:
        top = "\n".join(f"  {t.unit_id} [{t.kind}] {t.title} ({t.n_children})" for t in out.toc)
        body = (
            f"{head}\nedition {out.edition_label or out.edition}; pages {out.pages}; units {out.units}\n"
            f"PDF (host path): {pdf}\nMarkdown (host path): {md}\n"
            + (f"URL: {out.url}\nNOTE: {out.message}\n" if fmt == "pdf" else "")
            + (f"top-level structure:\n{top}" if top else "")
        )
    return _ok(body, out.model_dump(mode="json"))


# ============================================================================ images
def _png_for_model(path: Path) -> tuple[bytes | None, str]:
    """PNG bytes within ``MAX_IMAGE_BYTES`` (downscaled when needed) or ``None`` plus the reason."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        return None, f"cannot read the image file ({type(e).__name__})"
    if len(raw) <= MAX_IMAGE_BYTES:
        return raw, ""
    try:
        from PIL import Image
    except ImportError:
        return None, f"image is {len(raw)} bytes (> {MAX_IMAGE_BYTES}) and Pillow is not installed to downscale it"
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
    except Exception as e:  # noqa: BLE001 - damaged image: report, do not raise
        return None, f"cannot downscale the image ({type(e).__name__})"
    if buf.tell() > MAX_IMAGE_BYTES:
        return None, f"even downscaled the image is {buf.tell()} bytes (> {MAX_IMAGE_BYTES})"
    return buf.getvalue(), f"downscaled from {len(raw)} to {buf.tell()} bytes (max side {MAX_IMAGE_SIDE}px)"


def _image_result(head: str, r: kb.ImageResult) -> CallToolResult:
    path = Path(r.path) if r.path else None
    lines = [head]
    if r.caption:
        lines.append(f"caption: {r.caption}")
    if path:
        lines.append(f"file (host path): {_host_fig(str(path))}")
    if r.evidence is not None:
        lines.append(_fmt_ev(_ev(r.evidence), text=False))
    content: list[Any] = []
    if path:
        data, note = _png_for_model(path)
        if data is not None:
            content.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/png"))
            if note:
                lines.append(f"image: {note}")
        else:
            lines.append(f"image NOT attached: {note}. Open the file path above with the file Read tool.")
    return CallToolResult(content=[TextContent(type="text", text="\n".join(lines)), *content])


FIGURE_DESC = (
    "The PNG crop of ONE FIGURE of a document, returned as an image together with the figure's provenance (citation, "
    "caption, host path of the PNG). Use when a figure's content matters (flow charts, layouts, dimensions, decision "
    "trees) and the text of the document only mentions it. figure_id like 'Fig. 2.1.1' or 'rys. 2.1.1'; "
    "kb_toc with kind figure lists them. Too large an image is downscaled; if it still does not fit only the host "
    "path is returned (open it with the file Read tool). This tool returns content blocks without structuredContent."
)


def kb_figure(
    doc_id: DocIdArg,
    figure_id: Annotated[str, Field(min_length=1, max_length=60, description='e.g. "Fig. 2.1.1" or "2.1.1".')],
) -> CallToolResult:
    with _db() as conn:
        r = kb.figure(conn, doc_id, figure_id)
        indexed = _indexed(conn)
    if not r.ok:
        _fail(f"{r.message or f'no such figure in {C.doc_label(doc_id)}'}. Indexed documents: {', '.join(indexed)}.")
    return _image_result(f"{C.doc_label(doc_id)} {r.ref}", r)


PAGE_DESC = (
    "Rendered PNG of ONE PDF PAGE (1-based). Use to read a table or layout that the text extraction garbled, or to "
    "see a page as printed. Page numbers are internal locators (a hit's page_start), never part of a citation. "
    "Returned as an image plus the file's host path; too large an image is downscaled. Content blocks only "
    "(no structuredContent)."
)


def kb_page_image(
    doc_id: DocIdArg, page: Annotated[int, Field(ge=1, le=5000, description="Page number, 1-based.")]
) -> CallToolResult:
    with _db() as conn:
        try:
            r = kb.page_image(conn, doc_id, page)
        except Exception as e:  # noqa: BLE001 - a PDF that cannot be rendered is reported, not raised
            _fail(
                localize(
                    f"cannot render page {page} of {C.doc_label(doc_id)} ({type(e).__name__}); open the PDF with kb_get_document fmt=pdf"
                )
            )
        indexed = _indexed(conn)
    if not r.ok:
        _fail(f"{r.message or f'no page {page} in {C.doc_label(doc_id)}'}. Indexed documents: {', '.join(indexed)}.")
    return _image_result(f"{C.doc_label(doc_id)} page {page}", r)


# ============================================================================ verify
VERIFY_DESC = (
    "Verbatim check of a quotation: is `quote` in unit `ref` of document `doc_id` AND in the PDF text layer? Whitespace, "
    "hyphenation and typographic quotes are normalised; letters and digits must match exactly. Verdict OK or NO MATCH. "
    "On NO MATCH it names the unit that really contains the text (with its citation) and the closest passage found. "
    "Use right before a quotation goes into a mail, report or opinion; a paraphrase or a number remembered wrongly "
    "('256 mm' instead of '265 mm') fails. Needs at least 8 significant characters; max 3000. A NO MATCH is a normal "
    "result, not a tool error."
)


def kb_verify_quote(
    doc_id: DocIdArg,
    ref: Annotated[
        str,
        Field(
            min_length=1, max_length=80, description='The unit that is supposed to contain it, e.g. "Section 2.3.1".'
        ),
    ],
    quote: Annotated[
        str, Field(min_length=1, max_length=MAX_QUOTE, description="The text to check, as it will be quoted.")
    ],
) -> Annotated[CallToolResult, VerifyOut]:
    with _db() as conn:
        r = verify_quote(conn, doc_id, ref, quote)
        where: list[str] = []
        for uid in r.found_in_units[:5]:
            it = kb.lookup(conn, doc_id, uid).items[0]
            if it.evidence is not None:
                where.append(it.evidence.citation_pl)
        near_cite = None
        if r.closest_unit_id:
            it = kb.lookup(conn, doc_id, r.closest_unit_id).items[0]
            near_cite = it.evidence.citation_pl if it.evidence else None
    ev = _ev_model(r.evidence) if r.evidence is not None else None
    out = VerifyOut(
        verdict="OK" if r.match else "NO MATCH",
        match=r.match,
        in_unit=r.in_unit,
        in_pdf=r.in_pdf,
        requested_ref=r.requested_ref,
        message=r.message,
        pdf_message=r.pdf_message,
        found_in_units=r.found_in_units,
        found_in_citations_pl=where,
        closest=(r.closest[:600] if r.closest else None),
        closest_ratio=r.closest_ratio,
        closest_unit_id=r.closest_unit_id,
        closest_citation_pl=near_cite,
        evidence=ev,
    )
    lines = [f"{out.verdict}: {r.message}"]
    if r.pdf_message:
        lines.append(f"PDF: {r.pdf_message}")
    if ev is not None:
        lines.append(f"checked unit: {ev.citation_pl}")
    if where:
        lines.append("the quote IS verbatim in: " + "; ".join(where))
    if not r.match and r.closest:
        lines.append(f"closest passage ({r.closest_ratio:.2f}) in {near_cite or r.closest_unit_id}:\n{out.closest}")
    return _ok("\n".join(lines), out.model_dump(mode="json"))


# ============================================================================ documents list
DOCUMENTS_DESC = (
    "List the documents of the knowledge base: number, title, edition and printed edition label, page/unit/figure "
    "counts and the catalog status against the document source (current, outdated, missing_locally, withdrawn, unknown). "
    "scope='indexed' (default) = the searchable documents; scope='all' also lists documents known only to the source catalog "
    "or present as a PDF but not indexed (not searchable here; kb_get_document gives the PDF path of those on disk). "
    "filter = substring of number or title. Use to see which editions an answer rests on."
)


def kb_documents(
    filter: Annotated[str | None, Field(max_length=100, description="Substring of number or title.")] = None,
    scope: Annotated[Literal["indexed", "all"], Field(description="indexed (default) | all")] = "indexed",
    limit: Annotated[int, Field(ge=1, le=200, description="Documents per page (also capped by size).")] = 100,
    cursor: CursorArg = None,
) -> Annotated[CallToolResult, DocumentsOut]:
    key = _args_key(filter, scope)
    offset, _ = _decode_cursor(cursor, "kb_documents", key)
    with _db() as conn:
        infos = {s.doc_id: s for s in kb.list_documents(conn)}
        cat = K.cached_catalog()
        status = {s.doc_id: s for s in K.catalog_status(conn, cat)}
    rows: list[DocumentRow] = []
    for ds in sorted({*status, *infos}, key=K.dotted_key):
        st, info = status.get(ds), infos.get(ds)
        if scope == "indexed" and info is None:
            continue
        title = (info.title if info else None) or (st.title if st else None)
        if filter and filter.lower() not in f"{ds} {title or ''}".lower():
            continue
        rows.append(
            DocumentRow(
                doc_id=ds,
                title=title,
                indexed=info is not None,
                edition=info.edition if info else (st.local_edition if st else None),
                edition_label=info.edition_label if info else (st.latest_edition_label if st else None),
                catalog_status=st.status if st else "unknown",
                latest_edition=st.latest_edition if st else None,
                local_pdf=st.local_pdf if st else None,
                index_stale=bool(st and st.index_stale),
                pages=info.pages if info else None,
                n_units=info.n_units if info else None,
                n_figures=info.n_figures if info else None,
            )
        )
    counts: dict[str, int] = {}
    for x in rows:
        counts[x.catalog_status] = counts.get(x.catalog_status, 0) + 1
    page = rows[offset : offset + limit]
    lines_ = [
        f"{x.doc_id}  {x.title or '?'}  | edition {x.edition_label or x.edition or '?'} | "
        f"{'indexed' if x.indexed else 'NOT indexed'} | catalog {x.catalog_status}"
        + (f" | latest {x.latest_edition}" if x.latest_edition and x.latest_edition != x.edition else "")
        + (" | INDEX STALE (newer PDF on disk)" if x.index_stale else "")
        for x in page
    ]
    k = _fit([(len(s) + 1, _jlen(x.model_dump())) for s, x in zip(lines_, page, strict=True)]) if page else 0
    nxt = offset + k if offset + k < len(rows) else None
    cur = _encode_cursor("kb_documents", key, nxt) if nxt is not None else None
    head = f"{len(rows)} documents (scope={scope}), showing {offset + 1}-{offset + k}; catalog status counts {counts}"
    text = head + "\n" + "\n".join(lines_[:k]) + (f"\n[more: call again with cursor={cur}]" if cur else "")
    data = DocumentsOut(
        documents=page[:k],
        total=len(rows),
        offset=offset,
        counts=counts,
        catalog_fetched_at=cat.fetched_at if cat else None,
        next_cursor=cur,
    ).model_dump(mode="json")
    return _ok(text, data)


# ============================================================================ citations
CITATIONS_DESC = (
    f"Validate the document citations found in a text (e.g. '{C.doc_label('4-12')} Section 2.3.1', "
    f"'{C.doc_label('11-7')} Table 2.1.1', Polish or English forms) against the CURRENT editions: OK / UNKNOWN UNIT (with the "
    "nearest or the probably renumbered ids) / DOCUMENT NOT INDEXED. Use on a draft before it leaves, or on old notes. "
    "It checks that the cited unit exists, not that the quoted content is right (kb_verify_quote does that). "
    "Text up to 50000 characters; the result is paged (next_cursor)."
)


def kb_check_citations(
    text: Annotated[
        str, Field(min_length=1, max_length=MAX_CITATION_TEXT, description="The text to scan for citations.")
    ],
    cursor: CursorArg = None,
) -> Annotated[CallToolResult, CitationsOut]:
    key = _args_key(hashlib.sha256(text.encode()).hexdigest())
    offset, _ = _decode_cursor(cursor, "kb_check_citations", key)
    with _db() as conn:
        res = [x.to_dict() for x in check_citations(conn, text)]
    counts = {s: sum(r["status"] == s for r in res) for s in ("OK", "UNKNOWN UNIT", "DOCUMENT NOT INDEXED")}
    page = [{k: r[k] for k in CitationRow.model_fields} for r in res[offset : offset + 200]]
    lines_ = [
        f"line {r['line']}: {r['citation']} -> {r['status']}" + (f" ({r['suggestion']})" if r["suggestion"] else "")
        for r in page
    ]
    k = _fit([(len(s) + 1, _jlen(r)) for s, r in zip(lines_, page, strict=True)]) if page else 0
    nxt = offset + k if offset + k < len(res) else None
    cur = _encode_cursor("kb_check_citations", key, nxt) if nxt is not None else None
    head = (
        f"{len(res)} citations: {counts['OK']} OK, {counts['UNKNOWN UNIT']} unknown unit, "
        f"{counts['DOCUMENT NOT INDEXED']} document not indexed" + ("" if res else " (none recognised in the text)")
    )
    body = (
        head + ("\n" + "\n".join(lines_[:k]) if k else "") + (f"\n[more: call again with cursor={cur}]" if cur else "")
    )
    data = CitationsOut(
        all_ok=bool(res) and counts["OK"] == len(res),
        total=len(res),
        offset=offset,
        counts=counts,
        results=[CitationRow(**r) for r in page[:k]],
        next_cursor=cur,
    ).model_dump(mode="json")
    return _ok(body, data)


# ============================================================================ registration
#: (name, title, description, function, open_world) in ``tools/list`` order; also listed by the dashboard.
TOOL_SPECS: list[tuple[str, str, str, Callable[..., Any], bool]] = [
    ("kb_lookup", "Exact lookup of a known unit", LOOKUP_DESC, kb_lookup, False),
    ("kb_toc", "Structure of a document or branch", TOC_DESC, kb_toc, False),
    ("kb_fulltext", "Keyword / exact-phrase search (free)", FULLTEXT_DESC, kb_fulltext, False),
    ("kb_search", "Semantic search (paid APIs, budgeted)", SEARCH_DESC, kb_search, True),
    ("kb_get_document", "Whole document / chapter / file paths", DOCUMENT_DESC, kb_get_document, False),
    ("kb_figure", "Image of a figure", FIGURE_DESC, kb_figure, False),
    ("kb_page_image", "Image of a PDF page", PAGE_DESC, kb_page_image, False),
    ("kb_verify_quote", "Verify a quotation verbatim", VERIFY_DESC, kb_verify_quote, False),
    ("kb_documents", "Documents, editions and catalog status", DOCUMENTS_DESC, kb_documents, False),
    ("kb_check_citations", "Validate citations in a text", CITATIONS_DESC, kb_check_citations, False),
]


def register_tools(server: MCPServer) -> None:
    """Attach the tools in a fixed order (deterministic ``tools/list``, spec: SHOULD)."""
    for name, title, desc, fn, open_world in TOOL_SPECS:
        name = localize(name)
        ann = ToolAnnotations(title=title, **RO, idempotent_hint=True, open_world_hint=open_world)
        server.tool(name=name, title=title, description=localize(desc), annotations=ann)(_audited(name)(fn))
