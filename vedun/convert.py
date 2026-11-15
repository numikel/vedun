"""PDF -> Markdown conversion of the documents (Docling, OCR off, table structure on).

Output per sheet: ``md/<ds>_<YYYYMM>.md`` with YAML front matter, ``<!-- page: N -->`` markers,
real heading levels rebuilt from clause numbering, tables as Markdown tables and figures as PNG
crops under ``figures/<ds>_<YYYYMM>/`` referenced by Markdown image links.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import config
from .units import CLAUSE_NUM_RE, ID_TOKEN, clause_depth

CONVERTER_NAME = "docling"
CONVERT_FORMAT = 3  # bump when the Markdown layout changes (invalidates reuse of converted files)
FIGURE_DPI = 170

_GENERIC_FURNITURE = [
    r"^Page\s+\d+(\s+of\s+\d+)?$",
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}$",
]


def is_boilerplate_line(text: str) -> bool:
    """A whole line of page furniture: page number, date line, running header (the bare document id),
    or a collection-specific line from ``config.BOILERPLATE_PATTERNS``."""
    t = text.strip()
    if config.doc_id_re().fullmatch(t):
        return True
    return any(re.match(p, t, re.I) for p in (*_GENERIC_FURNITURE, *config.BOILERPLATE_PATTERNS))


_SUP_RE = re.compile(r"(?<![A-Za-z])(ft|m|mm|cm|km)\s([23])(?=[\s)\]/.,;:%]|$)")
_SUP_CLOSE_RE = re.compile(r"((?:ft|m|mm|cm|km)[23])\s+\)")
_CAPTION_TABLE_RE = re.compile(r"^Table\s+[\w.\-]+", re.I)
_CAPTION_FIG_RE = re.compile(r"^Fig(?:ure)?\.?\s+[\w.\-]+", re.I)
_HEADING_STOP = re.compile(r"[.:;,]$")


def fix_superscripts(s: str) -> str:
    """Docling emits ``ft 2`` / ``m 2`` for superscripts; restore ``ft2`` / ``m2``."""
    s = _SUP_RE.sub(r"\1\2", s)
    return _SUP_CLOSE_RE.sub(r"\1)", s)


def clean_text(s: str) -> str:
    """Normalise whitespace and superscripts in one text block."""
    s = s.replace(" ", " ")
    s = re.sub(r"[ \t]+", " ", s).strip()
    return fix_superscripts(s)


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, streamed."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_pdf_name(path: Path) -> tuple[str, str]:
    """``3-26_202610.pdf`` -> (``3-26``, ``2026-10``); the id pattern comes from the corpus profile."""
    m = config.pdf_name_re().match(path.name)
    if not m:
        raise ValueError(f"unexpected document file name: {path.name}")
    return m.group(1), f"{m.group(2)[:4]}-{m.group(2)[4:]}"


def edition_tag(edition: str) -> str:
    """``2026-10`` -> ``202610``."""
    return edition.replace("-", "")


@dataclass
class Block:
    """One output block (paragraph, heading, table, figure) with its page span."""

    kind: str  # heading | para | table | figure
    text: str
    page: int
    page_end: int
    level: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConvertInfo:
    """Result of converting one PDF."""

    ds: str
    edition: str
    md_path: Path
    fig_dir: Path
    pages: int
    seconds: float
    seconds_per_page: float
    n_blocks: int
    n_figures: int
    sha256: str
    title: str
    docling_version: str
    edition_label: str = ""
    edition_label_source: str = ""
    source_path: str = ""


_CONVERTERS: dict[bool, Any] = {}


def get_converter(fast_tables: bool = False) -> Any:
    """Lazy, cached Docling converter: OCR off (born-digital PDFs), table structure on (ACCURATE or FAST)."""
    if fast_tables not in _CONVERTERS:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = False
        opts.do_table_structure = True
        opts.table_structure_options.mode = TableFormerMode.FAST if fast_tables else TableFormerMode.ACCURATE
        opts.table_structure_options.do_cell_matching = True
        conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
        conv.initialize_pipeline(InputFormat.PDF)  # load models outside the timed section
        _CONVERTERS[fast_tables] = conv
    return _CONVERTERS[fast_tables]


def _is_furniture(text: str, title: str, ds: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if is_boilerplate_line(t):
        return True
    if t.lower() == title.lower() or t == ds:
        return True
    return False


def _looks_like_heading(num: str, rest: str) -> bool:
    """Numbered line that is a heading rather than a recommendation."""
    if not rest or len(rest) > 140 or _HEADING_STOP.search(rest):
        return False
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", rest) if len(w) > 3]
    if not words:
        return False
    cap = sum(1 for w in words if w[0].isupper())
    return cap / len(words) >= 0.6


def _table_key(caption: str) -> str:
    """``Table 2.2.2. Title (continued)`` -> ``2.2.2`` (empty if no id)."""
    m = re.match(rf"^Table\s+({ID_TOKEN})", caption.strip(), re.I)
    return m.group(1).lower() if m else ""


def _table_md(table: Any, doc: Any) -> str:
    md = table.export_to_markdown(doc)
    rows = [ln for ln in md.splitlines() if ln.strip().startswith("|")]  # drop Docling's caption
    return fix_superscripts("\n".join(rows)).strip()


def _split_md_table(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.strip().startswith("|")]


def _norm_row(line: str) -> str:
    return re.sub(r"[\s|\-:]+", "", line).lower()


def _merge_table_md(first: str, cont: str) -> str:
    """Append a continuation table (next page) to ``first``, dropping a repeated header."""
    a, b = _split_md_table(first), _split_md_table(cont)
    if len(a) < 2 or len(b) < 2:
        return first
    header_same = _norm_row(a[0]) == _norm_row(b[0])
    b_rows = b[2:] if header_same else [b[0]] + b[2:]
    return "\n".join(a + b_rows)


def blocks_from_docling(doc: Any, title: str, ds: str, require_start: bool = True) -> list[Block]:
    """Turn a DoclingDocument into an ordered list of cleaned blocks (TOC and furniture removed).

    Content starts at the first "1.0" heading (what precedes it is title page and contents); a document with no
    such heading is kept whole rather than dropped.
    """
    items = [(it, lv) for it, lv in doc.iterate_items()]
    blocks: list[Block] = []
    started = not require_start  # becomes True at the first "1.0" heading (everything before = TOC/title)
    skip_caption_idx: set[int] = set()

    def page_of(it: Any) -> tuple[int, int]:
        if not it.prov:
            return (blocks[-1].page_end if blocks else 1,) * 2
        return it.prov[0].page_no, it.prov[-1].page_no

    def caption_for(idx: int, it: Any) -> str:
        caps = getattr(it, "captions", None) or []
        for c in caps:
            try:
                txt = c.resolve(doc).text
            except Exception:  # noqa: BLE001
                continue
            if txt:
                return clean_text(txt)
        for j in (idx + 1, idx - 1):
            if 0 <= j < len(items) and items[j][0].label.value == "caption":
                skip_caption_idx.add(j)
                return clean_text(items[j][0].text)
        return ""

    for idx, (it, _lv) in enumerate(items):
        label = it.label.value
        if idx in skip_caption_idx or label in ("page_header", "page_footer", "caption"):
            continue
        p0, p1 = page_of(it)
        if label == "table":
            if not started:
                continue
            cap = caption_for(idx, it)
            md = _table_md(it, doc)
            if not md:
                continue
            prev_t = next((b for b in reversed(blocks) if b.kind == "table"), None)
            key = _table_key(cap)
            same_id = bool(prev_t and key and key == _table_key(prev_t.extra.get("caption", "")))
            glued = bool(prev_t and not cap and blocks[-1] is prev_t and prev_t.page_end == p0 - 1)
            if prev_t is not None and (same_id or glued):
                prev_t.text = _merge_table_md(prev_t.text, md)  # table continued on a later page
                prev_t.page_end = max(prev_t.page_end, p1)
                continue
            blocks.append(Block("table", md, p0, p1, extra={"caption": cap}))
            continue
        if label == "picture":
            if not started:
                continue
            cap = caption_for(idx, it)
            bbox = it.prov[0].bbox if it.prov else None
            blocks.append(Block("figure", cap, p0, p1, extra={"caption": cap, "bbox": bbox}))
            continue
        text = clean_text(getattr(it, "text", "") or "")
        if label in ("document_index",) or not text:
            continue
        marker = (getattr(it, "marker", "") or "").strip()
        if label == "list_item" and marker:
            if CLAUSE_NUM_RE.fullmatch(marker.rstrip(".")) and not text.startswith(marker):
                text = f"{marker.rstrip('.')} {text}"
            elif re.fullmatch(r"[A-Za-z0-9]{1,2}\.", marker):
                text = f"{marker} {text}"
            elif marker in ("·", "•", "-", "–", "∙", "o"):
                text = f"- {text}"
        if _is_furniture(text, title, ds):
            continue
        m = re.match(rf"^({CLAUSE_NUM_RE.pattern})\.?\s+(.*)$", text, re.S)
        if not started:
            if m and m.group(1) in ("1.0", "1"):
                started = True
            else:
                continue
        if m:
            num, rest = m.group(1), m.group(2)
            if label == "section_header" or _looks_like_heading(num, rest):
                blocks.append(Block("heading", f"{num} {rest}", p0, p1, level=min(clause_depth(num) + 1, 6)))
            else:
                blocks.append(Block("para", f"{num} {rest}", p0, p1))
            continue
        if re.match(r"^APPENDIX\s+[A-Z]\b", text):
            blocks.append(Block("heading", text, p0, p1, level=2))
            continue
        blocks.append(Block("para", text, p0, p1))
    if not started:
        return blocks_from_docling(doc, title, ds, require_start=False)
    return blocks


def crop_figures(pdf_path: Path, blocks: list[Block], doc: Any, fig_dir: Path, ds: str, ed_tag: str) -> int:
    """Crop each figure block out of the PDF page at ~170 dpi and store PNGs. Returns count."""
    import pypdfium2 as pdfium

    figs = [b for b in blocks if b.kind == "figure"]
    if not figs:
        return 0
    fig_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    scale = FIGURE_DPI / 72.0
    page_cache: dict[int, Any] = {}
    n = 0
    for b in figs:
        pg = b.page
        if pg not in page_cache:
            page_cache[pg] = pdf[pg - 1].render(scale=scale).to_pil()
        img = page_cache[pg]
        bbox = b.extra.get("bbox")
        page_h = doc.pages[pg].size.height
        if bbox is not None:
            bb = bbox.to_top_left_origin(page_h)
            pad = 6
            box = (
                max(0, int((bb.l - pad) * scale)),
                max(0, int((bb.t - pad) * scale)),
                min(img.width, int((bb.r + pad) * scale)),
                min(img.height, int((bb.b + pad) * scale)),
            )
            crop = img.crop(box)
        else:
            crop = img
        n += 1
        name = f"fig_p{pg:03d}_{n:02d}.png"
        crop.save(fig_dir / name, optimize=True)
        b.extra["file"] = name
        b.extra["size"] = crop.size
    return n


def render_markdown(blocks: list[Block], front: dict[str, Any], fig_rel: str) -> str:
    """Serialise blocks to Markdown with page markers and YAML front matter."""
    out = ["---", yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip(), "---", ""]
    cur = 0
    for b in blocks:
        if b.page != cur:
            out.append(f"<!-- page: {b.page} -->")
            out.append("")
            cur = b.page
        if b.kind == "heading":
            out.append("#" * b.level + " " + b.text)
        elif b.kind == "table":
            if b.extra.get("caption"):
                out.append(f"**{b.extra['caption']}**")
                out.append("")
            out.append(b.text)
        elif b.kind == "figure":
            cap = b.extra.get("caption") or "Figure (uncaptioned)"
            out.append(f"**{cap}**")
            out.append("")
            if b.extra.get("file"):
                out.append(f"![{cap}]({fig_rel}/{b.extra['file']})")
        else:
            out.append(b.text)
        if b.page_end != cur:  # glued to the block: it runs on to this page
            out.append(f"<!-- page: {b.page_end} -->")
            cur = b.page_end
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def pdf_title(pdf_path: Path, ds: str) -> str:
    """Document title from the PDF metadata, minus the profile's ``title_strip_patterns``."""
    from .pdftext import metadata_title

    t = metadata_title(pdf_path).strip()
    for pat in config.TITLE_STRIP_PATTERNS:
        t = re.sub(pat.replace("{doc_id}", re.escape(ds)), "", t)
    return t.strip()


_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def edition_label(pdf_path: Path, edition: str) -> tuple[str, str]:
    """Edition label as printed (``January 2026, revised July 2026``) and its source.

    The profile's ``edition_label`` rules say which lines of the first pages carry the original and the revision
    date; without rules, or when the document prints none, the month and year of the file name are used (flagged
    in the second return value).
    """
    from .pdftext import page_texts

    y, mo = edition.split("-")
    fallback = f"{_MONTHS[int(mo) - 1]} {y}", "derived from file name (no date line printed on first pages)"
    rules = config.EDITION_LABEL
    if not rules:
        return fallback
    orig_re = config.month_regex(rules["original"]) if rules.get("original") else None
    rev_re = config.month_regex(rules["revision"]) if rules.get("revision") else None
    orig = interim = None
    src = "printed on the sheet"
    for text in page_texts(pdf_path, max_pages=int(rules.get("pages", 2))):
        for ln in text.splitlines():
            ln = ln.strip()
            if orig is None and orig_re and orig_re.fullmatch(ln):
                orig = ln
            if interim is None and rev_re and rev_re.fullmatch(ln):
                interim = ln
    if orig is None and interim and rules.get("original_in_history"):  # original date only in the revision history
        hist = re.compile(config.month_regex(rules["original_in_history"]).pattern, re.M)
        for text in page_texts(pdf_path):
            if m := hist.search(text):
                orig, src = m.group(1), "printed on the sheet (first page + revision history)"
                break
    if orig or interim:
        return ", ".join(x for x in (orig, interim) if x), src
    return fallback


def convert_pdf(pdf_path: Path, md_dir: Path, fig_root: Path) -> ConvertInfo:
    """Convert one PDF; writes the Markdown file and figure crops. Does not touch the DB."""
    import docling

    ds, edition = parse_pdf_name(pdf_path)
    tag = edition_tag(edition)
    title = pdf_title(pdf_path, ds)
    sha = sha256_file(pdf_path)
    label, label_src = edition_label(pdf_path, edition)
    conv = get_converter(fast_tables=ds in config.FAST_TABLE_DOCUMENTS)
    t0 = time.perf_counter()
    res = conv.convert(str(pdf_path))
    doc = res.document
    n_pages = len(doc.pages)
    blocks = blocks_from_docling(doc, title, ds)
    fig_dir = fig_root / f"{ds}_{tag}"
    n_fig = crop_figures(pdf_path, blocks, doc, fig_dir, ds, tag)
    seconds = time.perf_counter() - t0
    front = {
        "ds": ds,
        "format": CONVERT_FORMAT,
        "title": title,
        "edition": edition,
        "edition_label": label,
        "edition_label_source": label_src,
        "source_pdf": pdf_path.name,
        "source_sha256": sha,
        "converter": f"{CONVERTER_NAME} {getattr(docling, '__version__', None) or _docling_version()}",
        "converted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pages": n_pages,
        "convert_seconds": round(seconds, 1),
        "seconds_per_page": round(seconds / max(n_pages, 1), 2),
    }
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / f"{ds}_{tag}.md"
    md_path.write_text(render_markdown(blocks, front, f"../figures/{ds}_{tag}"), encoding="utf-8")
    return ConvertInfo(
        ds,
        edition,
        md_path,
        fig_dir,
        n_pages,
        seconds,
        seconds / max(n_pages, 1),
        len(blocks),
        n_fig,
        sha,
        title,
        front["converter"],
        label,
        label_src,
        str(pdf_path.resolve()),
    )


def _docling_version() -> str:
    from importlib.metadata import version

    return version("docling")
