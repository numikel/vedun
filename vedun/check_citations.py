"""Validate document citations in notes (Polish or English) against the indexed editions.

Usage: ``python -m vedun check <file.md|.txt> [...]`` or ``python -m vedun check-citations --repo DIR``.
Exit code 1 if any citation points to a unit that does not exist in the current edition.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import config as C
from .search import lookup
from .units import canonical_clause


def anchor_re() -> re.Pattern[str]:
    """Document anchor of a citation: ``<prefix> <id>`` ("Doc 4-12"); both parts come from the corpus profile.

    The prefix is case-insensitive; the id keeps its own case, so "Doc 4-12a" anchors 4-12, not a document "4-12a".
    """
    return re.compile(rf"(?i:{C.CITATION_PREFIX_PATTERN})\s*({C.DOC_ID_PATTERN})(?![\d])")


_ID = r"(?:\d+|[A-Z])(?:\.\d+)+"
_TID = r"[A-Z]?\d+[a-z]?(?:\.\d+)*[a-z]?"
_SEP = r"(?:\s*\([^()]{0,100}\))?\s*(?:,|/|;|\bi\b|\boraz\b|\band\b|\bor\b|\blub\b|&)\s*\**\s*"
LOC_RE = re.compile(
    rf"(?P<loc>\b(?:pkt|punkt\w*|sec(?:tion|t)?|clause|tab(?:le|ela|ele|l)?|fig(?:ure)?|rys(?:unek|unku)?)\b\.?)\s*\**\s*"
    rf"(?P<ids>(?:{_TID}|{_ID})(?:{_SEP}(?:{_TID}|{_ID}))*)",
    re.I,
)
_ONE_ID = re.compile(_TID + r"|" + _ID)
_EDITION_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b"
    r"|\b(\d{4})-(\d{2})\b",
    re.I,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
WINDOW = 220


@dataclass
class Citation:
    """One citation found in a text."""

    line: int
    ds: str
    unit_id: str
    text: str  # as written, e.g. "Doc 4-12 clause 2.3.1"
    cited_edition: str | None = None


@dataclass
class CitationResult:
    """Validation outcome for one citation (JSON-serialisable)."""

    file: str
    line: int
    citation: str
    doc_id: str
    unit_id: str
    status: str  # OK | UNKNOWN UNIT | DOCUMENT NOT INDEXED
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unit_id_for(loc: str, raw: str) -> str:
    l = loc.lower()
    raw = raw.strip().rstrip(".")
    if l.startswith("tab"):
        return f"Table {raw}"
    if l.startswith(("fig", "rys")):
        return f"Figure {raw}"
    return canonical_clause(raw)


def extract_citations(text: str) -> list[Citation]:
    """Find all ``<prefix> <doc id> ... clause/table/figure <id>`` citations; one Citation per cited id."""
    anchors = list(anchor_re().finditer(text))
    out: list[Citation] = []
    for i, a in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        end = min(end, a.end() + WINDOW)
        window = text[a.end() : end]
        para = re.search(r"\n\s*\n", window)  # a citation does not cross a paragraph break
        if para:
            window = window[: para.start()]
        ed = None
        em = _EDITION_RE.search(window[:60])
        if em:
            if em.group(1):
                ed = f"{em.group(2)}-{_MONTHS[em.group(1).lower()]:02d}"
            else:
                ed = f"{em.group(3)}-{em.group(4)}"
        # a second locator list must start before any sentence that restarts the topic
        for lm in LOC_RE.finditer(window):
            loc = lm.group("loc")
            for n, im in enumerate(_ONE_ID.finditer(lm.group("ids"))):
                raw = im.group(0)
                is_clause_loc = loc.lower().startswith(("pkt", "punkt", "sec", "clause"))
                if is_clause_loc and "." not in raw:
                    continue  # bare "pkt 2" is not a clause id
                if n > 0 and raw.count(".") < 2:
                    continue  # list continuation: "pkt 2.2.1, 1.5 m" must not read "1.5" as a clause
                line = text.count("\n", 0, a.end() + lm.start()) + 1
                cited = f"{a.group(0).strip()} {loc} {raw}".strip()
                out.append(Citation(line, a.group(1), _unit_id_for(loc, raw), cited, ed))
    # de-duplicate same (line, ds, id)
    seen: set[tuple[int, str, str]] = set()
    uniq = []
    for c in out:
        key = (c.line, c.ds, c.unit_id)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def validate(conn: sqlite3.Connection, cites: list[Citation], file: str = "") -> list[CitationResult]:
    """Check each citation against the current editions in the DB."""
    res = []
    cache: dict[tuple[str, str], Any] = {}
    for c in cites:
        key = (c.ds, c.unit_id)
        if key not in cache:
            cache[key] = lookup(conn, c.ds, c.unit_id).items[0]
        it = cache[key]
        if "is not indexed" in it.message:
            res.append(CitationResult(file, c.line, c.text, c.ds, c.unit_id, "DOCUMENT NOT INDEXED"))
        elif it.found and it.evidence:
            note = ""
            if c.cited_edition and c.cited_edition != it.evidence.edition:
                note = f"note: cited edition {c.cited_edition}, current {it.evidence.edition}"
            res.append(CitationResult(file, c.line, c.text, c.ds, c.unit_id, "OK", note))
        else:
            sug = []
            if it.possibly_renumbered_to:
                sug.append("renumbered to " + ", ".join(it.possibly_renumbered_to))
            if it.nearest:
                sug.append("nearest: " + ", ".join(it.nearest[:4]))
            res.append(CitationResult(file, c.line, c.text, c.ds, c.unit_id, "UNKNOWN UNIT", "; ".join(sug)))
    return res


def check_citations(conn: sqlite3.Connection, text: str) -> list[CitationResult]:
    """Find and validate all document citations in ``text`` (tool: ``check_citations``)."""
    return validate(conn, extract_citations(text))


# Never notes that cite documents: tooling, caches and the knowledge base's own output. The profile adds the
# folders of its corpus (config ``citation_skip_dirs``: the PDF folder, the project folder under another name).
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".staging", "reports", "vedun"}


def skip_dirs() -> set[str]:
    """Folder names that ``iter_text_files`` never enters (built-in + profile)."""
    return _SKIP_DIRS | set(C.CITATION_SKIP_DIRS)


MAX_FILE_BYTES = 5_000_000


def iter_text_files(root: Path) -> list[Path]:
    """``*.md`` / ``*.txt`` files below ``root`` (skipping venvs, VCS, big files, the KB's own output)."""
    out = []
    skip = skip_dirs()
    # os.walk with in-place pruning: skipped trees (.venv, .git, node_modules ...) are never entered. A plain
    # rglob walks them first and filters afterwards, which takes minutes on a Docker bind mount.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in (".md", ".txt"):
                continue
            parts = set(p.parts)
            if parts & skip:
                continue
            try:
                if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(p)
    return sorted(out)


def check_files(conn: sqlite3.Connection, files: list[Path], base: Path | None = None) -> list[CitationResult]:
    """Validate all citations in the given files."""
    results: list[CitationResult] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = str(f.relative_to(base)) if base and f.is_relative_to(base) else str(f)
        results += validate(conn, extract_citations(text), name)
    return results


def format_table(results: list[CitationResult], only_problems: bool = False) -> str:
    """Plain table ``file:line | citation | status | suggestion``."""
    rows = [r for r in results if not only_problems or r.status != "OK" or r.suggestion]
    lines = ["file:line | citation | status | suggestion", "---|---|---|---"]
    lines += [f"{r.file}:{r.line} | {r.citation} | {r.status} | {r.suggestion}" for r in rows]
    return "\n".join(lines)
