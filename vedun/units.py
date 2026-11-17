"""Clause-id parsing and Markdown -> units (clauses, tables, figures, appendix sections).

Pure functions, no I/O except :func:`parse_markdown_file`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: A clause number: ``1.0``, ``2.3.1.4`` or an appendix style ``A.1.2``.
CLAUSE_NUM_RE = re.compile(r"(?:\d+|[A-Z])(?:\.\d+)+")
#: id token of a table/figure: ``2.4.1.1``, ``3.3-2``, ``9a``, ``2.3.1.6A``, ``C-1``
_SUFFIX = r"(?:[a-z]|[A-Z](?![a-z]))?"  # "6A" is a suffix, "6 And" is not
ID_TOKEN = (
    rf"(?:[A-Z]{{1,3}}[.\-]\d+{_SUFFIX}(?:[.\-]\d+{_SUFFIX})*"  # "C-1", "A.1.2", "AB-1"
    rf"|[A-Za-z]{{0,2}}\d+{_SUFFIX}(?:[.\-](?:\d+{_SUFFIX}|[A-Z]{{1,2}}(?![a-z])))*)"  # "6-US", "6-SI"
)
TABLE_TOKEN = rf"{ID_TOKEN}(?:\([a-z0-9]{{1,2}}\))?"  # "2.4.1.1.2.1(a)": some publishers number table variants this way
_TABLE_ID_RE = re.compile(rf"^Table\s+({TABLE_TOKEN})(?=[\s.:]|$)", re.I)
#: A caption the converter left unbolded: "Table 2.1.2.3.2. Water-Miscible ..." (not "Table 3.4.5 provides ...")
#: id may carry an upper-case prefix word ("Table AB A-3-3.1.2"); title after ". ", ": ", " - ", " . " or ".. "
_PLAIN_TABLE_CAPTION_RE = re.compile(
    rf"^Table\s+((?:[A-Z]{{1,3}}\s)?{TABLE_TOKEN})"
    r"(?:\s*[.:]+\s*(?=[A-Z0-9(\[])|\s+[-–]\s+(?=[A-Z])|\s+(?=[A-Z(\[]))"
)
_MAX_CAPTION_CHARS = 600  # a caption may carry a reproduction notice ("reproduced with permission of …")
_FIG_ID_RE = re.compile(rf"^Fig(?:ure)?\.?\s*({ID_TOKEN})(?=[\s.]|$)", re.I)
_PAGE_RE = re.compile(r"^<!-- page: (\d+) -->$")
IMG_RE = re.compile(r"^!\[(.*?)\]\((.+?)\)$")  # one figure link per line; db.reuse_converted shares it
_APPENDIX_RE = re.compile(r"^APPENDIX\s+([A-Z])\b\s*(.*)$")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TOC_LEADER_RE = re.compile(r"\.{6,}\s*\d")  # table-of-contents dot leaders


def _alt(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) or r"(?!)"


def _glued_re(units: tuple[str, ...]) -> re.Pattern[str]:
    """A clause number inside a paragraph, e.g. "...RECOMMENDATIONS 2.1 Equipment and Processes 2.1.1 ...".

    PDF converters often merge a whole page into one text item, so clause starts are not block starts.
    A number followed by a value unit ("2.5 MMBtuh") is not a clause; a clause title starts with a capital.
    """
    return re.compile(rf"(?<=\s)(\d{{1,2}}(?:\.\d{{1,3}}){{1,7}})\.?\s+(?!{_alt(units)})(?=[A-Z\"“])")


def _ref_before_re(words: tuple[str, ...]) -> re.Pattern[str]:
    """Text ending in a cross-reference word ("see", "Table"): the number after it is a reference."""
    return re.compile(rf"(?<![A-Za-z])(?:{_alt(words)})\s*$", re.I)  # "section 3.0" as well as "Section 3.0"


_LEAD_NUM_RE = re.compile(r"^(?:#{1,6}\s+)?(\d{1,2}(?:\.\d{1,3}){1,7})\.?\s")


def _num_parts(num: str) -> list[int]:
    return [int(x) for x in num.split(".")]


def next_clause_plausible(last: str | None, num: str) -> bool:
    """True if ``num`` can follow clause ``last`` in document order (child, sibling or ancestor's sibling).

    Small gaps (up to +3) are allowed: a clause may sit inside a table or be numbered with a typo.
    """
    n = _num_parts(num)
    if last is None:
        return n[:2] in ([1, 0], [1, 1])
    lst = _num_parts(last)
    if n == lst:
        return False
    child = lst[:-1] + [1] if len(lst) == 2 and lst[-1] == 0 else lst + [1]
    nexts = [child, lst + [0], [lst[0] + 1, 0], [lst[0] + 1, 1]]
    nexts += [lst[:k] + [lst[k] + step] for k in range(len(lst)) for step in (1, 2, 3)]
    nexts += [lst[:k] + [lst[k] + 1, 1] for k in range(len(lst))]  # the sheet skipped a heading (2.3.2.2 -> 2.3.2.3.1)
    return n in nexts


def split_glued(
    blk: str, last: str | None, ref_words: tuple[str, ...] | None = None, value_units: tuple[str, ...] | None = None
) -> list[str]:
    """Split a text block at clause numbers glued inside it, accepting only numbers that continue the sequence.

    ``last`` is the last clause id seen before this block; a block that itself starts with a number restarts
    the sequence there (the parser opens that clause regardless). ``ref_words`` / ``value_units`` default to the
    corpus profile (``config.CLAUSE_REF_WORDS`` / ``config.VALUE_UNITS``).
    """
    from . import config as C

    glued = _glued_re(C.VALUE_UNITS if value_units is None else value_units)
    ref = _ref_before_re(C.CLAUSE_REF_WORDS if ref_words is None else ref_words)
    lead = _LEAD_NUM_RE.match(blk)
    offset = lead.end() if lead else 0
    if lead:
        last = lead.group(1)
    pieces, start = [], 0
    for m in glued.finditer(blk, offset):
        num = m.group(1)
        if ref.search(blk[max(0, m.start() - 16) : m.start()]) or not next_clause_plausible(last, num):
            continue
        pieces.append(blk[start : m.start()].rstrip())
        start, last = m.start(), num
    pieces.append(blk[start:])
    return [p for p in pieces if p.strip()]


def clause_depth(num: str) -> int:
    """Depth of a clause number; ``1.0`` and ``2`` are depth 1, ``2.4.1`` is depth 3."""
    parts = num.split(".")
    if len(parts) == 2 and parts[1] == "0":
        return 1
    return len(parts)


def parent_id(unit_id: str) -> str | None:
    """Parent clause id: ``2.3.1.4`` -> ``2.3.1``; ``2.4`` -> ``2.0``; ``2.0`` -> None."""
    if unit_id.startswith(("Table ", "Figure ", "Appendix ")):
        return None
    parts = unit_id.split(".")
    if len(parts) == 2 and parts[1] == "0":
        return None
    if len(parts) == 2:
        return f"{parts[0]}.0"
    return ".".join(parts[:-1])


def canonical_clause(raw: str) -> str:
    """Canonical clause id (strip trailing dot / whitespace)."""
    return raw.strip().rstrip(".")


def canonical_table(raw: str) -> str:
    """``Table 2.4.1.4.1.`` / ``table 2.4.1.4.1`` / ``Tab. 2.4.1.4.1`` -> ``Table 2.4.1.4.1``."""
    m = re.match(r"^\s*(?:Table|Tab\.?)\s+(\S+?)\.?\s*$", raw, re.I)
    return f"Table {m.group(1)}" if m else raw.strip()


def canonical_figure(raw: str) -> str:
    """``Fig. 2.4.3`` -> ``Figure 2.4.3``."""
    m = re.match(r"^\s*(?:Figure|Fig\.?)\s+(\S+?)\.?\s*$", raw, re.I)
    return f"Figure {m.group(1)}" if m else raw.strip()


def sort_key(unit_id: str) -> tuple:
    """Natural ordering key for unit ids."""
    parts = re.split(r"[.\s]", unit_id)
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts if p)


@dataclass
class Unit:
    """One addressable unit of a document."""

    ds: str
    edition: str
    unit_id: str  # canonical id: "2.3.1.4", "Table 2.4.1.1", "Figure 2.4.3.1.1", "Appendix A"
    kind: str  # clause | table | figure | appendix
    parent: str | None
    title: str  # heading text, or first words of the line
    text: str  # own text (excluding children)
    page_start: int
    page_end: int
    seq: int = 0  # document order
    dup_index: int = 0  # >0 if the source repeats the same id (typo in the source)
    md_line_start: int = 0  # 1-based line range of the unit in the converted Markdown file
    md_line_end: int = 0
    extra: dict = field(default_factory=dict)  # figure: file, caption


def split_front_matter(md: str) -> tuple[dict, str]:
    """Split YAML front matter from a Markdown document."""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return yaml.safe_load(md[3:end]) or {}, md[end + 4 :].lstrip("\n")
    return {}, md


def _is_page_marker(lines: list[str]) -> bool:
    return len(lines) == 1 and bool(_PAGE_RE.match(lines[0].strip()))


def _first_words(text: str, n: int = 12) -> str:
    return " ".join(text.split()[:n])


def parse_markdown(md: str, ds: str | None = None, edition: str | None = None) -> tuple[dict, list[Unit]]:
    """Parse converted Markdown into units with page spans.

    Returns ``(front_matter, units)``. Own text of a clause runs up to the next unit start
    (sibling, child, table, figure or appendix heading); lettered items (``A.``) and bullets
    stay inside their parent clause because they are never unit starts.
    """
    front, body = split_front_matter(md)
    ds = ds or front.get("ds", "")
    edition = edition or front.get("edition", "")
    units: list[Unit] = []
    cur: Unit | None = None
    page = 0
    seen: dict[str, int] = {}
    last_owner: str | None = None  # last clause or appendix: parent of the tables and figures that follow it
    uncaptioned: dict[int, int] = {}
    last_unit: Unit | None = None  # last clause/appendix unit: owner of text after tables/figures
    blk_lines = [0, 0]  # 1-based file line range of the block being processed
    seq: list[str | None] = [None]  # last numeric clause id, for splitting glued paragraphs
    next_is_table = [False]  # the next block (page markers skipped) is a Markdown table
    body_line0 = md[: len(md) - len(body)].count("\n")  # lines before the body in the file

    def start(unit_id: str, kind: str, title: str, text: str, caption_or_extra: dict | None = None) -> None:
        nonlocal cur, last_owner, last_unit
        n = seen.get(unit_id, 0)
        seen[unit_id] = n + 1
        # an appendix is a top-level part of the document, never a child of the clause printed before it
        parent = parent_id(unit_id) if kind == "clause" else None if kind == "appendix" else last_owner
        cur = Unit(
            ds=ds,
            edition=edition,
            unit_id=unit_id,
            kind=kind,
            parent=parent,
            title=title,
            text=text,
            page_start=page,
            page_end=page,
            seq=len(units),
            dup_index=n,
            extra=caption_or_extra or {},
            md_line_start=blk_lines[0],
            md_line_end=blk_lines[1],
        )
        units.append(cur)
        if kind in ("clause", "appendix"):
            last_unit = cur
            last_owner = unit_id
        if kind == "clause":
            if unit_id[0].isdigit():
                seq[0] = unit_id

    def process(blk: str, lines: list[str]) -> Unit | None:
        """Handle one block; returns the unit that owns it (for page-end bookkeeping)."""
        nonlocal cur
        first = lines[0].strip()
        hm = _HEAD_RE.match(first)
        head_text = hm.group(2) if hm else first
        # --- figure: **Figure x. caption**  followed by an image block
        cap_m = re.match(r"^\*\*(.*)\*\*$", first)
        if cap_m and (_FIG_ID_RE.match(cap_m.group(1)) or "uncaptioned" in cap_m.group(1)):
            cap = cap_m.group(1)
            fig_m = _FIG_ID_RE.match(cap)
            if fig_m:
                uid = f"Figure {fig_m.group(1)}"
            else:
                uncaptioned[page] = uncaptioned.get(page, 0) + 1
                uid = f"Figure @p{page}-{uncaptioned[page]}"
            start(uid, "figure", cap, cap, {"caption": cap})
            return cur
        if cur is not None and cur.kind == "figure" and IMG_RE.match(first):
            im = IMG_RE.match(first)
            cur.extra["file"] = Path(im.group(2)).name  # type: ignore[union-attr]
            return cur
        # --- table: **Table x. caption** + md table
        if cap_m and _TABLE_ID_RE.match(cap_m.group(1)):
            cap = cap_m.group(1)
            tid = _TABLE_ID_RE.match(cap).group(1)  # type: ignore[union-attr]
            start(f"Table {tid}", "table", cap, "\n".join(lines[1:]) if len(lines) > 1 else "", {"caption": cap})
            return cur
        caption = " ".join(ln.strip() for ln in lines)
        if next_is_table[0] and len(caption) <= _MAX_CAPTION_CHARS and (pm := _PLAIN_TABLE_CAPTION_RE.match(caption)):
            start(f"Table {pm.group(1)}", "table", caption, "", {"caption": caption})
            return cur
        if cur is not None and cur.kind == "table" and first.startswith("|") and not cur.text:
            cur.text = blk
            return cur
        if first.startswith("|"):  # uncaptioned table: attach to the current unit
            if cur is not None:
                cur.text += "\n" + blk
            return cur
        # --- appendix heading
        am = _APPENDIX_RE.match(head_text)
        if hm and am:
            start(f"Appendix {am.group(1)}", "appendix", head_text, head_text)
            return cur
        # --- numbered clause (heading or paragraph)
        cm = re.match(rf"^({CLAUSE_NUM_RE.pattern})\.?\s+(\S.*)$", head_text, re.S)
        if cm:
            num = canonical_clause(cm.group(1))
            rest = cm.group(2)
            title = _first_words(rest.split("\n")[0]) if not hm else rest.strip()
            body_txt = head_text + ("\n" + "\n".join(lines[1:]) if len(lines) > 1 and not hm else "")
            start(num, "clause", title, body_txt, {"heading": True} if hm else None)
            if hm and len(lines) > 1:
                cur.text += "\n" + "\n".join(lines[1:])  # type: ignore[union-attr]
            return cur
        # --- unnumbered text: belongs to the current unit (after a table/figure: the last clause)
        target = cur
        if cur is not None and cur.kind in ("table", "figure") and last_unit is not None:
            target = last_unit
        if target is not None:
            target.text += "\n\n" + blk if target.text else blk
            target.page_end = max(target.page_end, page)
        return target

    groups: list[tuple[int, int, list[str]]] = []
    cur_lines: list[str] = []
    first_idx = 0
    for i, ln in enumerate(body.split("\n")):
        if ln.strip():
            if not cur_lines:
                first_idx = i
            cur_lines.append(ln.rstrip())
        elif cur_lines:
            groups.append((first_idx, i - 1, cur_lines))
            cur_lines = []
    if cur_lines:
        groups.append((first_idx, first_idx + len(cur_lines) - 1, cur_lines))
    for k, (i0, i1, lines) in enumerate(groups):
        blk_lines[0], blk_lines[1] = body_line0 + i0 + 1, body_line0 + i1 + 1
        nxt = next((groups[j][2] for j in range(k + 1, len(groups)) if not _is_page_marker(groups[j][2])), None)
        next_is_table[0] = nxt is not None and nxt[0].lstrip().startswith("|")
        m = _PAGE_RE.match(lines[0].strip())
        if m and len(lines) == 1:
            page = int(m.group(1))
            continue
        end_page: int | None = None
        if len(lines) > 1 and (em := _PAGE_RE.match(lines[-1].strip())):
            end_page = int(em.group(1))  # marker glued to the END of a block: it runs on to that page
            lines = lines[:-1]
        blk = "\n".join(lines)
        first = lines[0].lstrip()
        if first.startswith(("|", "**", "![")):
            pieces = [blk]
        elif _TOC_LEADER_RE.search(blk):
            pieces = []  # table of contents: its numbers would open phantom clauses
        else:
            pieces = split_glued(blk, seq[0])
        owner = None
        for piece in pieces:
            owner = process(piece, piece.split("\n")) or owner
            if owner is not None:
                owner.md_line_end = max(owner.md_line_end, blk_lines[1])
        if end_page is not None:
            if owner is not None:
                owner.page_end = max(owner.page_end, end_page)
            page = end_page
    return front, units


def parse_markdown_file(path: Path) -> tuple[dict, list[Unit]]:
    """Read and parse a converted Markdown file."""
    return parse_markdown(path.read_text(encoding="utf-8"))


def is_clause_descendant(unit_id: str, anc: str) -> bool:
    """True if clause ``unit_id`` lies below clause ``anc`` (``2.0`` owns every ``2.x``)."""
    if unit_id == anc:
        return False
    if anc.endswith(".0"):
        return unit_id.split(".")[0] == anc.split(".")[0]
    return unit_id.startswith(anc + ".")


def descendants(units: list[Unit], unit_id: str) -> list[Unit]:
    """All descendants of clause ``unit_id`` in document order (clauses by id prefix, tables and
    figures by the clause they follow)."""
    clause_ids = {u.unit_id for u in units if u.kind == "clause" and is_clause_descendant(u.unit_id, unit_id)}
    out = []
    for u in units:
        if u.kind == "clause" and u.unit_id in clause_ids:
            out.append(u)
        elif u.kind in ("table", "figure", "appendix") and u.parent and (u.parent == unit_id or u.parent in clause_ids):
            out.append(u)
    return out


def ancestors(known_ids: set[str], unit_id: str) -> list[str]:
    """Existing ancestor clause ids of ``unit_id`` (outermost first), skipping numbering gaps."""
    out: list[str] = []
    p = parent_id(unit_id) if not unit_id.startswith(("Table ", "Figure ", "Appendix ")) else None
    while p:
        if p in known_ids:
            out.append(p)
        p = parent_id(p)
    return list(reversed(out))
