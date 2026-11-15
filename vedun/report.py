"""Edition-change report: which unit ids vanished, appeared, changed text or were renumbered.

Some publishers renumber clauses and tables between editions without saying so. The renumbering guess
matches *text*, not ids.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config as C
from .units import Unit, descendants, sort_key

if TYPE_CHECKING:
    from .db import SyncResult

#: text similarity (difflib ratio) at/above which old unit X is declared to now sit under Y
RENUMBER_THRESHOLD = 0.75
#: lower bound for "possible" renumbering candidates listed separately for human review
POSSIBLE_THRESHOLD = 0.60
_ID_PREFIX = re.compile(r"^\s*(?:(?:\d+|[A-Z])(?:\.\d+)+\.?\s+)")


@dataclass
class ChangedUnit:
    """A unit whose id stayed but whose text changed."""

    unit_id: str
    diff: str


@dataclass
class Renumber:
    """Old unit ``old_id`` appears to sit now under ``new_id``."""

    old_id: str
    new_id: str
    score: float
    old_title: str = ""
    new_title: str = ""


@dataclass
class DiffReport:
    """Difference between two editions of one sheet (JSON-serialisable)."""

    doc_id: str
    old_edition: str | None
    new_edition: str
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    changed: list[ChangedUnit] = field(default_factory=list)
    renumbered: list[Renumber] = field(default_factory=list)
    possible: list[Renumber] = field(default_factory=list)
    unchanged: int = 0
    changes_section: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize(text: str) -> str:
    """Comparison form: drop leading clause id, collapse whitespace, lowercase."""
    t = _ID_PREFIX.sub("", text, count=1)
    t = re.sub(r"^\s*\*\*.*?\*\*\s*", "", t) if t.lstrip().startswith("**") else t
    return re.sub(r"\s+", " ", t).strip().lower()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[./][0-9]+)?", text)


def short_diff(old: str, new: str, limit: int = 360) -> str:
    """Word-level diff with ``[-removed-]`` / ``{+added+}`` markers, trimmed around the edits."""
    a, b = old.split(), new.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            seg = a[i1:i2]
            parts.append(" ".join(seg) if len(seg) <= 6 else " ".join(seg[:3]) + " … " + " ".join(seg[-3:]))
        else:
            if i2 > i1:
                parts.append("[-" + " ".join(a[i1:i2]) + "-]")
            if j2 > j1:
                parts.append("{+" + " ".join(b[j1:j2]) + "+}")
    out = " ".join(parts)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _body(u_text: str, kind: str, title: str) -> str:
    if kind in ("table", "figure"):
        return normalize(title.split(".", 1)[-1] if kind == "table" else title) + " || " + normalize(u_text)
    return normalize(u_text)


def _score(old_n: str, new_n: str, ow: set[str], nw: set[str]) -> float:
    if not ow or not nw:
        return 0.0
    jac = len(ow & nw) / len(ow | nw)
    if jac < 0.3:
        return 0.0
    return difflib.SequenceMatcher(None, old_n, new_n, autojunk=False).ratio()


def diff_editions(
    old_units: list[dict[str, Any]], new_units: list[Unit], old_edition: str | None, new_edition: str
) -> DiffReport:
    """Compare the units of the previous edition (DB rows) with the freshly parsed new ones."""
    ds = new_units[0].ds if new_units else (old_units[0]["doc_id"] if old_units else "")
    rep = DiffReport(doc_id=ds, old_edition=old_edition, new_edition=new_edition)
    old = {u["unit_id"]: u for u in old_units if u["dup_index"] == 0}
    new = {u.unit_id: u for u in new_units if u.dup_index == 0}
    rep.removed = sorted(set(old) - set(new), key=sort_key)
    rep.added = sorted(set(new) - set(old), key=sort_key)
    for uid in sorted(set(old) & set(new), key=sort_key):
        o, n = old[uid], new[uid]
        if normalize(o["text"]) == normalize(n.text) and o["kind"] == n.kind:
            rep.unchanged += 1
        else:
            rep.changed.append(ChangedUnit(uid, short_diff(normalize(o["text"]), normalize(n.text))))
    # --- renumbering guess: text of a vanished id found under a new id (1:1, greedy by score)
    cand_new = {i: new[i] for i in rep.added}
    new_prep = {
        i: (_body(u.text, u.kind, u.title), set(_words(_body(u.text, u.kind, u.title)))) for i, u in cand_new.items()
    }
    pairs: list[tuple[float, str, str]] = []
    for oid in rep.removed:
        ou = old[oid]
        on = _body(ou["text"], ou["kind"], ou["title"])
        ow = set(_words(on))
        if len(ow) < 3:
            continue
        for nid, (nn, nw) in new_prep.items():
            if cand_new[nid].kind != ou["kind"]:
                continue
            s = _score(on, nn, ow, nw)
            if s >= POSSIBLE_THRESHOLD:
                pairs.append((s, oid, nid))
    pairs.sort(reverse=True)
    used_o: set[str] = set()
    used_n: set[str] = set()
    for s, oid, nid in pairs:
        if oid in used_o or nid in used_n:
            continue
        used_o.add(oid)
        used_n.add(nid)
        r = Renumber(oid, nid, round(s, 3), (old[oid]["title"] or "")[:80], (new[nid].title or "")[:80])
        (rep.renumbered if s >= RENUMBER_THRESHOLD else rep.possible).append(r)
    rep.renumbered.sort(key=lambda r: sort_key(r.old_id))
    rep.possible.sort(key=lambda r: sort_key(r.old_id))
    rep.changes_section = changes_section(new_units)
    return rep


def changes_section(units: list[Unit]) -> str:
    """Verbatim text of the document's own "what changed" section (with sub-units).

    The profile's ``changes_unit_id`` names it (e.g. "1.2"); otherwise the first clause titled "Changes".
    """
    cid = C.CHANGES_UNIT_ID
    root = next((u for u in units if cid and u.unit_id == cid and u.dup_index == 0), None)
    if root is None or "change" not in root.title.lower():
        root = next((u for u in units if u.kind == "clause" and "changes" in u.title.lower()[:20]), None)
    if root is None:
        return ""
    parts = [root.text.strip()]
    parts += [u.text.strip() for u in descendants(units, root.unit_id) if u.kind == "clause"]
    return "\n\n".join(p for p in parts if p)


def render_diff(rep: DiffReport) -> str:
    """Markdown section for one replaced document."""
    L = [f"## Document {rep.doc_id}: {rep.old_edition} -> {rep.new_edition}", ""]
    L.append(
        f"{len(rep.removed)} ids disappeared, {len(rep.added)} new, {len(rep.changed)} with changed "
        f"text, {rep.unchanged} unchanged, {len(rep.renumbered)} renumbering matches "
        f"(text similarity >= {RENUMBER_THRESHOLD}), {len(rep.possible)} weaker candidates "
        f"({POSSIBLE_THRESHOLD}-{RENUMBER_THRESHOLD})."
    )
    L.append("")
    if rep.renumbered:
        L += [
            "### Probable renumbering (old id -> new id)",
            "",
            "| old | new | similarity | heading |",
            "|---|---|---|---|",
        ]
        L += [f"| {r.old_id} | {r.new_id} | {r.score:.2f} | {r.new_title or r.old_title} |" for r in rep.renumbered]
        L.append("")
    if rep.possible:
        L += ["### Weaker candidates (review by hand)", "", "| old | new | similarity |", "|---|---|---|"]
        L += [f"| {r.old_id} | {r.new_id} | {r.score:.2f} |" for r in rep.possible]
        L.append("")
    renumbered_old = {r.old_id for r in rep.renumbered}
    renumbered_new = {r.new_id for r in rep.renumbered}
    gone = [i for i in rep.removed if i not in renumbered_old]
    fresh = [i for i in rep.added if i not in renumbered_new]
    L += ["### Ids that disappeared (no text match found)", "", ", ".join(gone) or "none", ""]
    L += ["### New ids (no old counterpart)", "", ", ".join(fresh) or "none", ""]
    if rep.changed:
        L += ["### Same id, changed text", ""]
        L += [f"- **{c.unit_id}**: {c.diff}" for c in rep.changed]
        L.append("")
    if rep.changes_section:
        L += [
            "### The document's own changes section (verbatim)",
            "",
            "> " + rep.changes_section.replace("\n", "\n> "),
            "",
        ]
    return "\n".join(L)


def write_sync_report(res: SyncResult, report_dir: Path) -> Path:
    """Write ``reports/sync_<date>.md`` for a sync run; returns the path."""
    report_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = report_dir / f"sync_{now:%Y-%m-%d}.md"
    if path.exists():
        path = report_dir / f"sync_{now:%Y-%m-%d_%H%M}.md"
    L = [f"# vedun sync report {now:%Y-%m-%d %H:%M}", ""]
    L.append(
        f"Added: {', '.join(r.doc_id for r in res.results if r.action == 'added') or '-'}; "
        f"replaced: {', '.join(r.doc_id for r in res.results if r.action == 'replaced') or '-'}; "
        f"removed (PDF gone): {', '.join(res.removed) or '-'}; "
        f"unchanged: {len(res.plan.unchanged)}."
    )
    L.append("")
    for w in res.plan.warnings:
        L.append(f"WARNING: {w}")
    L.append("")
    for r in res.results:
        if r.diff is not None:
            L.append(render_diff(r.diff))
        else:
            L.append(
                f"## Document {r.doc_id}: new document, edition {r.new_edition} ({r.n_units} units, {r.n_chunks} chunks)"
            )
            L.append("")
        for m in r.models:
            if m.status not in ("complete", "pending"):
                L.append(f"- embedding {m.model_key}/{m.kind}: {m.status} ({m.n_ok}/{m.n_expected}) {m.error}")
        L.append("")
    for ds in res.removed:
        L.append(f"## Document {ds}: removed (PDF no longer in the folder)")
        L.append("")
    for ds, err in res.failed.items():
        L.append(f"## Document {ds}: FAILED, previous state kept (run sync again for this document)")
        L.append("")
        L.append(f"- {err}")
        L.append("")
    if res.cancelled:
        L.append(f"## Cancelled: {len(res.skipped)} document(s) not processed, previous state kept (run sync again)")
        L.append("")
        L.append(", ".join(res.skipped) or "(none left)")
        L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
    return path
