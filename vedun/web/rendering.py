"""Safe HTML rendering of verbatim unit text: highlights, Markdown tables, diff markers."""

from __future__ import annotations

import re
from collections.abc import Sequence

from markupsafe import Markup, escape

_MARK_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_SEP_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def marked_terms(snippet: str | None) -> list[str]:
    """Words the FTS5 ``snippet()`` marked with ``**...**``, longest first, de-duplicated."""
    if not snippet:
        return []
    found = {m.strip() for m in _MARK_RE.findall(snippet) if m.strip()}
    return sorted(found, key=lambda t: (-len(t), t.lower()))


def snippet_html(snippet: str | None) -> Markup:
    """An FTS5 snippet with ``**term**`` marks as escaped HTML with ``<mark>``."""
    if not snippet:
        return Markup("")
    out: list[str] = []
    pos = 0
    for m in _MARK_RE.finditer(snippet):
        out.append(str(escape(snippet[pos : m.start()])))
        out.append("<mark>" + str(escape(m.group(1))) + "</mark>")
        pos = m.end()
    out.append(str(escape(snippet[pos:])))
    return Markup("".join(out))


def highlight(text: str, terms: Sequence[str]) -> Markup:
    """HTML-escape ``text`` and wrap every whole-word occurrence of ``terms`` in ``<mark>``."""
    if not text:
        return Markup("")
    if not terms:
        return Markup(escape(text))
    rx = re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.I)
    out: list[str] = []
    pos = 0
    for m in rx.finditer(text):
        out.append(str(escape(text[pos : m.start()])))
        out.append("<mark>" + str(escape(m.group(0))) + "</mark>")
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return Markup("".join(out))


def _cells(line: str) -> list[str]:
    s = line.strip()
    s = s.removeprefix("|").removesuffix("|")
    return [c.strip() for c in s.split("|")]


def _table_html(block: list[str], terms: Sequence[str]) -> str:
    rows = [r for r in block if not _SEP_ROW.match(r)]
    has_head = len(block) > 1 and _SEP_ROW.match(block[1]) is not None
    parts = ['<div class="table-scroll"><table class="md-table">']
    for i, r in enumerate(rows):
        tag = "th" if (has_head and i == 0) else "td"
        if has_head and i == 0:
            parts.append("<thead>")
        elif has_head and i == 1:
            parts.append("<tbody>")
        elif not has_head and i == 0:
            parts.append("<tbody>")
        parts.append("<tr>" + "".join(f"<{tag}>{highlight(c, terms)}</{tag}>" for c in _cells(r)) + "</tr>")
        if has_head and i == 0:
            parts.append("</thead>")
    parts.append("</tbody></table></div>" if rows else "</table></div>")
    return "".join(parts)


def render_text(text: str, terms: Sequence[str] = ()) -> Markup:
    """Verbatim text as paragraphs; Markdown tables become real tables. Always escaped."""
    if not text:
        return Markup("")
    blocks: list[str] = []
    para: list[str] = []
    table: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append('<p class="ev-p">' + str(highlight("\n".join(para).strip(), terms)) + "</p>")
            para.clear()

    def flush_table() -> None:
        if table:
            blocks.append(_table_html(list(table), terms))
            table.clear()

    for line in text.split("\n"):
        if line.strip().startswith("|"):
            flush_para()
            table.append(line)
        elif not line.strip():
            flush_table()
            flush_para()
        else:
            flush_table()
            para.append(line)
    flush_table()
    flush_para()
    return Markup("".join(blocks))


_DEL_RE = re.compile(r"\[-(.+?)-\]", re.S)
_INS_RE = re.compile(r"\{\+(.+?)\+\}", re.S)


def diff_html(diff: str) -> Markup:
    """``[-removed-]`` / ``{+added+}`` markers of the sync report as ``<del>`` / ``<ins>``."""
    s = str(escape(diff))
    s = _DEL_RE.sub(lambda m: f"<del>{m.group(1)}</del>", s)
    s = _INS_RE.sub(lambda m: f"<ins>{m.group(1)}</ins>", s)
    return Markup(s)
