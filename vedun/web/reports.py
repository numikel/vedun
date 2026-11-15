"""Reader for the Markdown edition-change reports written by ``vedun.report.write_sync_report``.

The reports are plain Markdown with a fixed layout (see ``render_diff``); this module turns one
into dataclasses the template can render as tables and word diffs. Unknown lines are ignored, an
unreadable file gives ``None`` - never an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from markupsafe import Markup

from .i18n import t
from .rendering import diff_html

NAME_RE = re.compile(r"^[\w][\w.\-]*\.md$")


@dataclass
class RenumberRow:
    old: str
    new: str
    score: float
    heading: str = ""


@dataclass
class ChangedRow:
    unit_id: str
    diff: Markup


@dataclass
class SheetSection:
    ds: str
    kind: str  # replaced | new | removed
    old_edition: str | None = None
    new_edition: str | None = None
    headline: str = ""
    n_gone: int = 0
    n_new: int = 0
    n_changed: int = 0
    n_unchanged: int = 0
    n_renumbered: int = 0
    n_possible: int = 0
    renumbered: list[RenumberRow] = field(default_factory=list)
    possible: list[RenumberRow] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    fresh: list[str] = field(default_factory=list)
    changed: list[ChangedRow] = field(default_factory=list)
    changes_text: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    name: str
    generated: str = ""
    added: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)
    sections: list[SheetSection] = field(default_factory=list)
    mtime: float = 0.0
    size: int = 0


_SUMMARY = re.compile(
    r"Added:\s*(?P<added>.*?);\s*replaced:\s*(?P<replaced>.*?);\s*removed \(PDF gone\):\s*(?P<removed>.*?);\s*"
    r"unchanged:\s*(?P<unchanged>\d+)",
    re.S,
)
_COUNTS = re.compile(
    r"(\d+) ids disappeared, (\d+) new, (\d+) with changed text, (\d+) unchanged, (\d+) renumbering matches"
    r".*?, (\d+) weaker candidates"
)


def _split_list(s: str) -> list[str]:
    s = s.strip()
    if not s or s == "-" or s.lower() == "none":
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows = []
    for ln in lines:
        if not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows[1:]  # drop the header row


def _renumber_rows(lines: list[str]) -> list[RenumberRow]:
    out = []
    for c in _table_rows(lines):
        if len(c) < 3:
            continue
        try:
            score = float(c[2])
        except ValueError:
            continue
        out.append(RenumberRow(c[0], c[1], score, c[3] if len(c) > 3 else ""))
    return out


def _parse_section(head: str, body: list[str]) -> SheetSection | None:
    m = re.match(r"^## (?:Document|DS) (\S+?):\s*(.*)$", head)  # "DS": the heading of older reports
    if not m:
        return None
    ds, rest = m.group(1), m.group(2)
    if rest.startswith("removed"):
        return SheetSection(ds, "removed", headline=t("Document removed (no PDF file in the folder)"))
    if rest.startswith(("new document", "new sheet")):
        ed = re.search(r"edition (\S+)", rest)
        n = re.search(r"\((\d+) units, (\d+) chunks\)", rest)
        sec = SheetSection(ds, "new", new_edition=ed.group(1) if ed else None)
        sec.headline = (
            t("New document: {units} units, {chunks} chunks", units=n.group(1), chunks=n.group(2))
            if n
            else t("New document")
        )
        sec.notes = [ln[2:] for ln in body if ln.startswith("- embedding")]
        return sec
    ed = re.match(r"^(.*?)\s*->\s*(.*)$", rest)
    sec = SheetSection(ds, "replaced", old_edition=ed.group(1) if ed else None, new_edition=ed.group(2) if ed else rest)
    # split the body into ### subsections
    subs: list[tuple[str, list[str]]] = [("", [])]
    for ln in body:
        if ln.startswith("### "):
            subs.append((ln[4:].strip(), []))
        else:
            subs[-1][1].append(ln)
    if (c := _COUNTS.search("\n".join(subs[0][1]))) is not None:
        sec.n_gone, sec.n_new, sec.n_changed, sec.n_unchanged, sec.n_renumbered, sec.n_possible = (
            int(x) for x in c.groups()
        )
    for title, lines in subs[1:]:
        low = title.lower()
        joined = " ".join(x.strip() for x in lines if x.strip())
        if low.startswith("probable renumbering"):
            sec.renumbered = _renumber_rows(lines)
        elif low.startswith("weaker candidates"):
            sec.possible = _renumber_rows(lines)
        elif low.startswith("ids that disappeared"):
            sec.gone = _split_list(joined)
        elif low.startswith("new ids"):
            sec.fresh = _split_list(joined)
        elif low.startswith("same id"):
            for ln in lines:
                cm = re.match(r"^- \*\*(.+?)\*\*: (.*)$", ln)
                if cm:
                    sec.changed.append(ChangedRow(cm.group(1), diff_html(cm.group(2))))
        elif low.startswith(("the document's own", "sheet's own")):
            sec.changes_text = "\n".join(re.sub(r"^>\s?", "", ln) for ln in lines if ln.startswith(">")).strip()
    sec.notes = [ln[2:] for ln in body if ln.startswith("- embedding")]
    return sec


def parse_report(text: str, name: str = "") -> Report:
    """Parse the Markdown of one sync report."""
    rep = Report(name)
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        rep.generated = re.sub(r"^#\s+.*?sync report\s*", "", lines[0]).strip()  # any brand before "sync report"
    if (m := _SUMMARY.search("\n".join(lines[:8]))) is not None:
        rep.added = _split_list(m.group("added"))
        rep.replaced = _split_list(m.group("replaced"))
        rep.removed = _split_list(m.group("removed"))
        rep.unchanged = int(m.group("unchanged"))
    rep.warnings = [ln.removeprefix("WARNING:").strip() for ln in lines if ln.startswith("WARNING:")]
    head: str | None = None
    body: list[str] = []
    for ln in [*lines, "## Document __end__:"]:
        if re.match(r"## (?:Document|DS) ", ln):
            if head is not None and (sec := _parse_section(head, body)) is not None and sec.ds != "__end__":
                rep.sections.append(sec)
            head, body = ln, []
        elif head is not None:
            body.append(ln)
    return rep


def list_reports(report_dir: Path) -> list[Report]:
    """Header-level info for every ``sync_*.md`` in ``report_dir``, newest first."""
    out: list[Report] = []
    try:
        files = sorted(report_dir.glob("sync_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for p in files:
        try:
            head = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[:8])
            rep = parse_report(head, p.name)
            rep.mtime, rep.size = p.stat().st_mtime, p.stat().st_size
            out.append(rep)
        except OSError:
            continue
    return out


def read_report(report_dir: Path, name: str) -> Report | None:
    """The fully parsed report ``name`` (a plain file name inside ``report_dir``), else None."""
    if not NAME_RE.match(name):
        return None
    p = (report_dir / name).resolve()
    if p.parent != report_dir.resolve() or not p.is_file():
        return None
    try:
        rep = parse_report(p.read_text(encoding="utf-8", errors="replace"), name)
        rep.mtime, rep.size = p.stat().st_mtime, p.stat().st_size
    except OSError:
        return None
    return rep
