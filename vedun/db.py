"""SQLite storage: schema, chunking, embedding orchestration, replace/remove/sync, doctor.

One edition per sheet number (``ds``). Every table row carries ``ds`` and ``edition``.
Replacing a sheet is one transaction: all old rows (units, FTS5, chunks, every vec0 table) are
deleted, the new edition is inserted, and only after COMMIT the old Markdown file and figure
folder are removed.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlite_vec

from . import config as C
from .convert import ConvertInfo, convert_pdf, edition_tag, parse_pdf_name, sha256_file
from .embed import BudgetExceeded, Embedder, EmbeddingError, TimeBudgetExceeded, get_embedder
from .report import DiffReport, diff_editions
from .units import Unit, parse_markdown_file

SCHEMA_VERSION = "4"  # 4: sheets/ds -> documents/doc_id, fm_item_id -> source_item_id (see migrate)
FIGURE_JOINT_CAPTION = False  # image-only vectors give a true cross-modal test


# ------------------------------------------------------------------------------ connection
def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the DB (creating it) with sqlite-vec loaded and the schema ensured."""
    p = Path(path) if path else C.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=60, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    # Rollback journal, not WAL: the host CLI and the container share this file over a bind mount, and WAL's
    # shared memory does not work across that boundary. A large page cache keeps a sheet's write transaction
    # from spilling to disk early, so the EXCLUSIVE lock (which blocks readers) is taken only at COMMIT.
    # shortcut: 128 MB cache per writer; a sheet whose rows exceed it spills and blocks readers again.
    conn.execute("PRAGMA cache_size=-131072")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    migrate(conn)  # before init_schema: on an old file it would create empty new tables next to the full old ones
    init_schema(conn)
    return conn


def needs_migration(conn: sqlite3.Connection) -> bool:
    """True for a schema-3 file (table ``sheets``): open it once with :func:`connect` or run ``vedun migrate``."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheets'").fetchone() is not None


def migrate(conn: sqlite3.Connection, log: Callable[[str], None] = lambda s: None) -> bool:
    """Schema 3 -> 4 in one transaction: ``sheets``/``ds`` -> ``documents``/``doc_id``, ``fm_item_id`` ->
    ``source_item_id``. Plain tables are renamed in place; FTS5 and vec0 tables cannot rename a column, so they are
    rebuilt and their rows (vectors included) copied. Returns False when there is nothing to do.
    """
    if not needs_migration(conn):
        return False
    virtual = {
        r["name"]: r["sql"]
        for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'")
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE sheets RENAME TO documents")
        conn.execute("ALTER TABLE documents RENAME COLUMN fm_item_id TO source_item_id")
        conn.execute("ALTER TABLE sheet_models RENAME TO document_models")
        for table in ("documents", "document_models", "units", "chunks", "renames"):
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN ds TO doc_id")
        for name, sql in virtual.items():
            if name != "units_fts" and not name.startswith(("vec_", "figvec_")):
                continue
            new_sql = sql.replace(" ds ", " doc_id ")
            if new_sql.count("doc_id") != 1:
                raise RuntimeError(f"migrate: unexpected definition of {name}: {sql}")
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
            # rebuilt under its own name via a temp copy: renaming a vec0 table leaves its shadow tables behind
            rowid = "rowid, " if name == "units_fts" else ""
            conn.execute(f"CREATE TEMP TABLE _migrate AS SELECT {rowid}{', '.join(cols)} FROM {name}")
            n_old = conn.execute("SELECT COUNT(*) FROM _migrate").fetchone()[0]
            conn.execute(f"DROP TABLE {name}")
            conn.execute(new_sql)
            new_cols = ", ".join("doc_id" if c == "ds" else c for c in cols)
            conn.execute(f"INSERT INTO {name}({rowid}{new_cols}) SELECT {rowid}{', '.join(cols)} FROM _migrate")
            conn.execute("DROP TABLE _migrate")
            n_new = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            if n_old != n_new:
                raise RuntimeError(f"migrate: {name} copied {n_new} of {n_old} rows")
            log(f"migrate: rebuilt {name} ({n_new} rows)")
        conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    log("migrate: schema 3 -> 4 done")
    return True


def vec_table(model_key: str) -> str:
    """Name of the text-chunk vec0 table of a model."""
    return f"vec_{model_key}"


def figvec_table(model_key: str) -> str:
    """Name of the figure-image vec0 table of a model."""
    return f"figvec_{model_key}"


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables (idempotent). vec0 tables are created for every registered model."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS documents(
            doc_id TEXT PRIMARY KEY, edition TEXT NOT NULL, title TEXT, source_pdf TEXT,
            source_sha256 TEXT, md_file TEXT, fig_dir TEXT, pages INTEGER, converter TEXT,
            convert_seconds REAL, seconds_per_page REAL, converted_at TEXT, indexed_at TEXT,
            n_units INTEGER, n_chunks INTEGER, n_figures INTEGER,
            edition_label TEXT, edition_label_source TEXT, source_path TEXT, source_item_id TEXT);
        CREATE TABLE IF NOT EXISTS document_models(
            doc_id TEXT NOT NULL, edition TEXT NOT NULL, model_key TEXT NOT NULL, kind TEXT NOT NULL,
            model_id TEXT, dim INTEGER, status TEXT, n_expected INTEGER, n_ok INTEGER, error TEXT,
            PRIMARY KEY(doc_id, model_key, kind));
        CREATE TABLE IF NOT EXISTS units(
            id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, edition TEXT NOT NULL, unit_id TEXT NOT NULL,
            dup_index INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL, parent TEXT, title TEXT,
            text TEXT, page_start INTEGER, page_end INTEGER, seq INTEGER, extra TEXT,
            md_line_start INTEGER, md_line_end INTEGER,
            UNIQUE(doc_id, unit_id, dup_index));
        CREATE INDEX IF NOT EXISTS units_ds ON units(doc_id);
        CREATE INDEX IF NOT EXISTS units_parent ON units(doc_id, parent);
        CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, edition TEXT NOT NULL,
            unit_row INTEGER NOT NULL, chunk_no INTEGER NOT NULL, text TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS chunks_ds ON chunks(doc_id);
        CREATE INDEX IF NOT EXISTS chunks_unit ON chunks(unit_row);
        CREATE TABLE IF NOT EXISTS renames(
            doc_id TEXT NOT NULL, old_edition TEXT, new_edition TEXT NOT NULL, old_id TEXT NOT NULL,
            new_id TEXT NOT NULL, score REAL, PRIMARY KEY(doc_id, new_edition, old_id));
        CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
            title, text, uid, doc_id UNINDEXED, edition UNINDEXED,
            tokenize = 'porter unicode61 remove_diacritics 2');
        """
    )
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    for key, spec in C.MODELS.items():
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table(key)} USING vec0("
            f"chunk_id integer primary key, doc_id text, edition text, "
            f"embedding float[{spec.dim}] distance_metric=cosine)"
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {figvec_table(key)} USING vec0("
            f"unit_row integer primary key, doc_id text, edition text, "
            f"embedding float[{spec.dim}] distance_metric=cosine)"
        )
        conn.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (f"model_{key}", f"{spec.api_id}|{spec.dim}"))


# ------------------------------------------------------------------------------- chunking
def _words_to_tokens(n_words: int) -> int:
    return int(n_words / C.WORDS_PER_TOKEN)


def split_text(text: str, max_tokens: int = C.CHUNK_TOKENS, overlap_tokens: int = C.CHUNK_OVERLAP_TOKENS) -> list[str]:
    """Split long text into overlapping windows of about ``max_tokens`` tokens."""
    words = text.split()
    max_w = max(20, int(max_tokens * C.WORDS_PER_TOKEN))
    ov_w = int(overlap_tokens * C.WORDS_PER_TOKEN)
    if len(words) <= max_w:
        return [" ".join(words)] if words else []
    out, i = [], 0
    while i < len(words):
        out.append(" ".join(words[i : i + max_w]))
        if i + max_w >= len(words):
            break
        i += max_w - ov_w
    return out


def split_table(md: str, caption: str, max_tokens: int = C.CHUNK_TOKENS) -> list[str]:
    """Split a Markdown table into row groups, repeating the caption and header in each."""
    lines = [ln for ln in md.splitlines() if ln.strip()]
    if len(lines) < 3:
        return [(caption + "\n" + md).strip()]
    header, rows = lines[:2], lines[2:]
    max_w = int(max_tokens * C.WORDS_PER_TOKEN)
    base = len(" ".join(header).split()) + len(caption.split())
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_w = base
    for r in rows:
        w = len(r.split())
        if cur and cur_w + w > max_w:
            groups.append(cur)
            cur, cur_w = [], base
        cur.append(r)
        cur_w += w
    if cur:
        groups.append(cur)
    return [(caption + "\n" + "\n".join(header + g)).strip() for g in groups]


def context_prefix(u: Unit, sheet_title: str, by_id: dict[str, Unit]) -> str:
    """``<doc label> <title> — <ancestor headings> — <unit id> <heading>``."""
    anc: list[str] = []
    p, hops = u.parent, 0
    while p and hops < 8:
        pu = by_id.get(p)
        if pu is None:
            break
        if pu.extra.get("heading"):
            anc.append(f"{pu.unit_id} {pu.title}")
        p = pu.parent
        hops += 1
    anc = list(reversed(anc))[-3:]
    head = u.title if (u.kind != "clause" or u.extra.get("heading")) else ""
    return f"{C.doc_label(u.ds)} {sheet_title} — {' > '.join(anc)} — {u.unit_id} {head}".strip()


def chunk_unit(u: Unit, sheet_title: str, by_id: dict[str, Unit]) -> list[str]:
    """Chunks of one unit; every chunk starts with the contextual prefix."""
    prefix = context_prefix(u, sheet_title, by_id)
    if u.kind == "table":
        parts = split_table(u.text, u.extra.get("caption", u.title))
    else:
        parts = split_text(u.text)
    if not parts:
        parts = [u.title or u.unit_id]
    return [f"{prefix}\n{p}" for p in parts]


# --------------------------------------------------------------------------- dataclasses
@dataclass
class ModelOutcome:
    """Embedding outcome of one model for one sheet."""

    model_key: str
    kind: str  # text | image
    status: str  # complete | partial | failed | skipped
    n_expected: int
    n_ok: int
    error: str = ""


@dataclass
class ReplaceResult:
    """Outcome of :func:`replace_document` (JSON-serialisable via ``to_dict``)."""

    doc_id: str
    action: str  # added | replaced | unchanged | removed
    old_edition: str | None
    new_edition: str | None
    n_units: int = 0
    n_chunks: int = 0
    n_figures: int = 0
    convert_seconds: float = 0.0
    seconds_per_page: float = 0.0
    models: list[ModelOutcome] = field(default_factory=list)
    diff: DiffReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------- helpers
def _vec_blob(v: list[float]) -> bytes:
    return sqlite_vec.serialize_float32(v)


def load_units(conn: sqlite3.Connection, ds: str) -> list[dict[str, Any]]:
    """All units of a sheet as plain dicts (document order)."""
    rows = conn.execute("SELECT * FROM units WHERE doc_id=? ORDER BY seq", (ds,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["extra"] = json.loads(d["extra"] or "{}")
        out.append(d)
    return out


def get_sheet(conn: sqlite3.Connection, ds: str) -> dict[str, Any] | None:
    """Sheet row or None."""
    r = conn.execute("SELECT * FROM documents WHERE doc_id=?", (ds,)).fetchone()
    return dict(r) if r else None


def _delete_sheet_rows(conn: sqlite3.Connection, ds: str) -> None:
    """Delete every row of ``ds`` from every table (must run inside a transaction)."""
    conn.execute("DELETE FROM units_fts WHERE rowid IN (SELECT id FROM units WHERE doc_id=?)", (ds,))
    for key in C.MODELS:
        conn.execute(f"DELETE FROM {vec_table(key)} WHERE doc_id=?", (ds,))
        conn.execute(f"DELETE FROM {figvec_table(key)} WHERE doc_id=?", (ds,))
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (ds,))
    conn.execute("DELETE FROM units WHERE doc_id=?", (ds,))
    conn.execute("DELETE FROM document_models WHERE doc_id=?", (ds,))
    conn.execute("DELETE FROM documents WHERE doc_id=?", (ds,))


def _remove_files(ds: str, edition: str, md_dir: Path, fig_dir: Path) -> None:
    tag = edition_tag(edition)
    f = md_dir / f"{ds}_{tag}.md"
    if f.exists():
        f.unlink()
    d = fig_dir / f"{ds}_{tag}"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _embed_batches(
    embed_fn: Callable[[list[Any]], list[list[float]]], items: list[Any], batch: int
) -> tuple[list[list[float] | None], str]:
    """Embed in batches; on failure return what we have plus the error text."""
    out: list[list[float] | None] = [None] * len(items)
    err = ""
    for i in range(0, len(items), batch):
        try:
            vecs = embed_fn(items[i : i + batch])
        except (EmbeddingError, BudgetExceeded, TimeBudgetExceeded) as e:
            err = str(e)[:300]
            break
        for j, v in enumerate(vecs):
            out[i + j] = v
    return out, err


# --------------------------------------------------------------------- embedding payloads
@dataclass
class SheetPayload:
    """Everything needed to insert one sheet (computed before the transaction)."""

    info: ConvertInfo
    units: list[Unit]
    chunks: list[tuple[int, int, str]]  # (unit index, chunk_no, text)
    text_vecs: dict[str, list[list[float] | None]] = field(default_factory=dict)
    fig_vecs: dict[str, list[tuple[int, list[float] | None]]] = field(default_factory=dict)
    outcomes: list[ModelOutcome] = field(default_factory=list)


def embed_payload(
    payload: SheetPayload,
    text_models: list[str],
    image_models: list[str],
    embedders: dict[str, Embedder] | None = None,
) -> None:
    """Fill ``payload`` with text-chunk and figure vectors from the configured models."""
    embedders = embedders if embedders is not None else {}

    def emb(key: str) -> Embedder:
        if key not in embedders:
            embedders[key] = get_embedder(key)
        return embedders[key]

    texts = [c[2] for c in payload.chunks]
    for key in text_models:
        e = emb(key)
        e.start_run()
        vecs, err = _embed_batches(lambda b, e=e: e.embed_texts(b, role="doc"), texts, e.spec.batch_size)
        payload.text_vecs[key] = vecs
        n_ok = sum(v is not None for v in vecs)
        status = "complete" if n_ok == len(texts) else ("partial" if n_ok else "failed")
        payload.outcomes.append(ModelOutcome(key, "text", status, len(texts), n_ok, err))
    figs = [(i, u) for i, u in enumerate(payload.units) if u.kind == "figure" and u.extra.get("file")]
    fig_dir = payload.info.fig_dir
    for key in image_models:
        e = emb(key)
        e.start_run()
        paths = [fig_dir / u.extra["file"] for _, u in figs]
        caps = [u.extra.get("caption", "") if FIGURE_JOINT_CAPTION else "" for _, u in figs]

        def fn(batch: list[int], e: Embedder = e, paths: list[Path] = paths, caps: list[str] = caps):
            return e.embed_images([paths[i] for i in batch], [caps[i] for i in batch])

        vecs, err = _embed_batches(fn, list(range(len(figs))), 4)
        payload.fig_vecs[key] = [(figs[i][0], vecs[i]) for i in range(len(figs))]
        n_ok = sum(v is not None for v in vecs)
        status = "complete" if n_ok == len(figs) else ("partial" if n_ok else "failed")
        payload.outcomes.append(ModelOutcome(key, "image", status, len(figs), n_ok, err))


def _insert_payload(conn: sqlite3.Connection, p: SheetPayload, pdf_path: Path) -> None:
    """Insert one sheet (inside a transaction)."""
    info = p.info
    ds, ed = info.ds, info.edition
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_fig = sum(1 for u in p.units if u.kind == "figure")
    conn.execute(
        "INSERT INTO documents(doc_id,edition,title,source_pdf,source_sha256,md_file,fig_dir,pages,converter,"
        "convert_seconds,seconds_per_page,converted_at,indexed_at,n_units,n_chunks,n_figures,"
        "edition_label,edition_label_source,source_path,source_item_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ds,
            ed,
            info.title,
            pdf_path.name,
            info.sha256,
            info.md_path.name,
            info.fig_dir.name,
            info.pages,
            info.docling_version,
            info.seconds,
            info.seconds_per_page,
            now,
            now,
            len(p.units),
            len(p.chunks),
            n_fig,
            info.edition_label,
            info.edition_label_source,
            info.source_path or str(pdf_path.resolve()),
            None,
        ),
    )
    row_ids: list[int] = []
    for u in p.units:
        cur = conn.execute(
            "INSERT INTO units(doc_id,edition,unit_id,dup_index,kind,parent,title,text,page_start,"
            "page_end,seq,extra,md_line_start,md_line_end) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ds,
                ed,
                u.unit_id,
                u.dup_index,
                u.kind,
                u.parent,
                u.title,
                u.text,
                u.page_start,
                u.page_end,
                u.seq,
                json.dumps(u.extra, ensure_ascii=False),
                u.md_line_start,
                u.md_line_end,
            ),
        )
        rid = int(cur.lastrowid)  # type: ignore[arg-type]
        row_ids.append(rid)
        conn.execute(
            "INSERT INTO units_fts(rowid,title,text,uid,doc_id,edition) VALUES(?,?,?,?,?,?)",
            (rid, u.title, u.text, u.unit_id, ds, ed),
        )
    chunk_ids: list[int] = []
    for ui, cn, txt in p.chunks:
        cur = conn.execute(
            "INSERT INTO chunks(doc_id,edition,unit_row,chunk_no,text) VALUES(?,?,?,?,?)",
            (ds, ed, row_ids[ui], cn, txt),
        )
        chunk_ids.append(int(cur.lastrowid))  # type: ignore[arg-type]
    for key, vecs in p.text_vecs.items():
        for cid, v in zip(chunk_ids, vecs, strict=True):
            if v is not None:
                conn.execute(
                    f"INSERT INTO {vec_table(key)}(chunk_id,doc_id,edition,embedding) VALUES(?,?,?,?)",
                    (cid, ds, ed, _vec_blob(v)),
                )
    for key, items in p.fig_vecs.items():
        for ui, v in items:
            if v is not None:
                conn.execute(
                    f"INSERT INTO {figvec_table(key)}(unit_row,doc_id,edition,embedding) VALUES(?,?,?,?)",
                    (row_ids[ui], ds, ed, _vec_blob(v)),
                )
    for o in p.outcomes:
        spec = C.MODELS[o.model_key]
        conn.execute(
            "INSERT OR REPLACE INTO document_models VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ds, ed, o.model_key, o.kind, spec.api_id, spec.dim, o.status, o.n_expected, o.n_ok, o.error),
        )


# --------------------------------------------------------------------- replace / remove
def reuse_converted(pdf_path: Path, md_dir: Path, fig_dir: Path, stage: Path) -> ConvertInfo | None:
    """Reuse an existing conversion (same PDF sha256, same Markdown format): no Docling run.

    Copies ``md/<ds>_<ed>.md`` and its figure folder into the staging area and rebuilds the
    :class:`ConvertInfo` from the front matter. Returns None if nothing reusable exists.
    """
    from .convert import CONVERT_FORMAT
    from .units import IMG_RE, split_front_matter

    ds, edition = parse_pdf_name(pdf_path)
    tag = edition_tag(edition)
    md = md_dir / f"{ds}_{tag}.md"
    if not md.exists():
        return None
    front, _ = split_front_matter(md.read_text(encoding="utf-8"))
    if front.get("format") != CONVERT_FORMAT or front.get("source_sha256") != sha256_file(pdf_path):
        return None
    from .convert import edition_label

    label, label_src = edition_label(pdf_path, edition)  # cheap; keeps labels current with the rules
    (stage / "md").mkdir(parents=True, exist_ok=True)
    text = md.read_text(encoding="utf-8")
    if front.get("edition_label") != label:
        text = text.replace(f"edition_label: {front.get('edition_label')}", f"edition_label: {label}", 1)
        text = text.replace(
            f"edition_label_source: {front.get('edition_label_source')}", f"edition_label_source: {label_src}", 1
        )
    (stage / "md" / md.name).write_text(text, encoding="utf-8")
    src_fig = fig_dir / f"{ds}_{tag}"
    dst_fig = stage / "figures" / f"{ds}_{tag}"
    dst_fig.mkdir(parents=True, exist_ok=True)
    # same link rule as the unit parser: alt text may itself contain brackets ("[SMACNA]")
    referenced = {Path(m.group(2)).name for ln in text.splitlines() if (m := IMG_RE.match(ln.strip()))}
    if any(not (src_fig / name).exists() for name in referenced):
        shutil.rmtree(stage, ignore_errors=True)
        return None  # a figure is gone: reconvert rather than commit a sheet with a hole in it
    for name in referenced:  # only figures the Markdown refers to (no stale files)
        shutil.copyfile(src_fig / name, dst_fig / name)
    n_fig = len(referenced)
    return ConvertInfo(
        ds,
        edition,
        stage / "md" / md.name,
        dst_fig,
        int(front.get("pages", 0)),
        float(front.get("convert_seconds", 0)),
        float(front.get("seconds_per_page", 0)),
        0,
        n_fig,
        front["source_sha256"],
        front.get("title", ""),
        str(front.get("converter", "")),
        label,
        label_src,
        str(pdf_path.resolve()),
    )


def build_payload(info: ConvertInfo) -> SheetPayload:
    """Parse the converted Markdown of ``info`` into units and chunks (no embeddings yet)."""
    _front, units = parse_markdown_file(info.md_path)
    by_id = {u.unit_id: u for u in units if u.dup_index == 0}
    chunks: list[tuple[int, int, str]] = []
    for i, u in enumerate(units):
        for n, txt in enumerate(chunk_unit(u, info.title, by_id)):
            chunks.append((i, n, txt))
    return SheetPayload(info=info, units=units, chunks=chunks)


def replace_document(
    conn: sqlite3.Connection,
    pdf_path: Path,
    *,
    text_models: list[str] | None = None,
    image_models: list[str] | None = None,
    md_dir: Path | None = None,
    fig_dir: Path | None = None,
    embedders: dict[str, Embedder] | None = None,
    embed: bool = True,
    converter: Callable[[Path, Path, Path], ConvertInfo] = convert_pdf,
    log: Callable[[str], None] = lambda s: None,
) -> ReplaceResult:
    """Add or replace the sheet of ``pdf_path`` (one edition per ``ds``), atomically.

    Conversion and embedding happen first; the DB swap is one transaction. After COMMIT the
    old edition's Markdown file and figure folder are deleted.
    """
    md_dir = md_dir or C.MD_DIR
    fig_dir = fig_dir or C.FIG_DIR
    text_models = C.TEXT_MODEL_KEYS if text_models is None else text_models
    image_models = C.IMAGE_MODEL_KEYS if image_models is None else image_models
    ds, edition = parse_pdf_name(pdf_path)
    old = get_sheet(conn, ds)
    old_units = load_units(conn, ds) if old else []
    stage = md_dir.parent / ".staging" / ds
    if stage.exists():
        shutil.rmtree(stage)
    info = reuse_converted(pdf_path, md_dir, fig_dir, stage)
    if info is None:
        log(f"[{ds}] converting {pdf_path.name}")
        info = converter(pdf_path, stage / "md", stage / "figures")
    else:
        log(f"[{ds}] reusing existing conversion of {pdf_path.name}")
    payload = build_payload(info)
    log(f"[{ds}] {len(payload.units)} units, {len(payload.chunks)} chunks, {info.n_figures} figures")
    if not any(u.kind == "clause" for u in payload.units):
        log(f"[{ds}] WARNING: no numbered clause found; lookup by clause number will not work for this sheet")
    if embed:
        embed_payload(payload, text_models, image_models, embedders)
    else:
        for k in text_models:
            payload.outcomes.append(ModelOutcome(k, "text", "pending", len(payload.chunks), 0))
        n_f = sum(1 for u in payload.units if u.kind == "figure" and u.extra.get("file"))
        for k in image_models:
            payload.outcomes.append(ModelOutcome(k, "image", "pending", n_f, 0))
    diff = diff_editions(old_units, payload.units, old["edition"] if old else None, edition) if old else None
    # --- atomic swap
    new_md = md_dir / info.md_path.name
    new_fig = fig_dir / info.fig_dir.name
    md_dir.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _delete_sheet_rows(conn, ds)
        _insert_payload(conn, payload, pdf_path)
        if diff is not None:
            conn.execute("DELETE FROM renames WHERE doc_id=? AND new_edition=?", (ds, edition))
            for r in diff.renumbered:
                conn.execute(
                    "INSERT OR REPLACE INTO renames VALUES(?,?,?,?,?,?)",
                    (ds, diff.old_edition, edition, r.old_id, r.new_id, r.score),
                )
        shutil.copyfile(info.md_path, new_md)
        if new_fig.exists():
            shutil.rmtree(new_fig)
        if info.fig_dir.exists():
            shutil.copytree(info.fig_dir, new_fig)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if old and old["edition"] != edition:
        _remove_files(ds, old["edition"], md_dir, fig_dir)
    shutil.rmtree(stage, ignore_errors=True)
    return ReplaceResult(
        doc_id=ds,
        action="replaced" if old else "added",
        old_edition=old["edition"] if old else None,
        new_edition=edition,
        n_units=len(payload.units),
        n_chunks=len(payload.chunks),
        n_figures=info.n_figures,
        convert_seconds=info.seconds,
        seconds_per_page=info.seconds_per_page,
        models=payload.outcomes,
        diff=diff,
    )


def remove_document(conn: sqlite3.Connection, ds: str, md_dir: Path | None = None, fig_dir: Path | None = None) -> bool:
    """Remove a sheet completely (rows in all tables, Markdown file, figure folder)."""
    md_dir = md_dir or C.MD_DIR
    fig_dir = fig_dir or C.FIG_DIR
    old = get_sheet(conn, ds)
    if not old:
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        _delete_sheet_rows(conn, ds)
        conn.execute("DELETE FROM renames WHERE doc_id=?", (ds,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    _remove_files(ds, old["edition"], md_dir, fig_dir)
    return True


def heal_embeddings(
    conn: sqlite3.Connection,
    ds: str,
    text_models: list[str],
    image_models: list[str],
    embedders: dict[str, Embedder] | None = None,
    fig_dir: Path | None = None,
) -> list[ModelOutcome]:
    """Embed only the chunks/figures that lack vectors for the given models (retry path)."""
    fig_dir = fig_dir or C.FIG_DIR
    embedders = embedders if embedders is not None else {}
    sheet = get_sheet(conn, ds)
    if not sheet:
        return []
    ed = sheet["edition"]
    outcomes: list[ModelOutcome] = []

    def emb(key: str) -> Embedder:
        if key not in embedders:
            embedders[key] = get_embedder(key)
        return embedders[key]

    for key in text_models:
        rows = conn.execute(
            f"SELECT id, text FROM chunks WHERE doc_id=? AND id NOT IN (SELECT chunk_id FROM {vec_table(key)} WHERE doc_id=?)",
            (ds, ds),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (ds,)).fetchone()[0]
        if not rows:
            n_ok, err = total, ""
        else:
            e = emb(key)
            e.start_run()
            vecs, err = _embed_batches(
                lambda b, e=e: e.embed_texts(b, role="doc"), [r["text"] for r in rows], e.spec.batch_size
            )
            conn.execute("BEGIN IMMEDIATE")
            for r, v in zip(rows, vecs, strict=True):
                if v is not None:
                    conn.execute(
                        f"INSERT INTO {vec_table(key)}(chunk_id,doc_id,edition,embedding) VALUES(?,?,?,?)",
                        (r["id"], ds, ed, _vec_blob(v)),
                    )
            conn.execute("COMMIT")
            n_ok = total - len(rows) + sum(v is not None for v in vecs)
        status = "complete" if n_ok == total else ("partial" if n_ok else "failed")
        o = ModelOutcome(key, "text", status, total, n_ok, err)
        outcomes.append(o)
        _save_outcome(conn, ds, ed, o)
    figs = conn.execute("SELECT id, extra FROM units WHERE doc_id=? AND kind='figure'", (ds,)).fetchall()
    figs = [(r["id"], json.loads(r["extra"] or "{}")) for r in figs]
    figs = [(i, x) for i, x in figs if x.get("file")]
    for key in image_models:
        have = {r[0] for r in conn.execute(f"SELECT unit_row FROM {figvec_table(key)} WHERE doc_id=?", (ds,))}
        todo = [(i, x) for i, x in figs if i not in have]
        n_ok, err = len(figs) - len(todo), ""
        if todo:
            e = emb(key)
            e.start_run()
            sub = fig_dir / f"{ds}_{edition_tag(ed)}"
            paths = [sub / x["file"] for _, x in todo]
            vecs, err = _embed_batches(
                lambda b, e=e, paths=paths: e.embed_images([paths[i] for i in b]), list(range(len(todo))), 4
            )
            conn.execute("BEGIN IMMEDIATE")
            for (i, _x), v in zip(todo, vecs, strict=True):
                if v is not None:
                    conn.execute(
                        f"INSERT INTO {figvec_table(key)}(unit_row,doc_id,edition,embedding) VALUES(?,?,?,?)",
                        (i, ds, ed, _vec_blob(v)),
                    )
            conn.execute("COMMIT")
            n_ok += sum(v is not None for v in vecs)
        status = "complete" if n_ok == len(figs) else ("partial" if n_ok else "failed")
        o = ModelOutcome(key, "image", status, len(figs), n_ok, err)
        outcomes.append(o)
        _save_outcome(conn, ds, ed, o)
    return outcomes


def _save_outcome(conn: sqlite3.Connection, ds: str, ed: str, o: ModelOutcome) -> None:
    spec = C.MODELS[o.model_key]
    conn.execute(
        "INSERT OR REPLACE INTO document_models VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ds, ed, o.model_key, o.kind, spec.api_id, spec.dim, o.status, o.n_expected, o.n_ok, o.error),
    )


# ------------------------------------------------------------------------------- sync


@dataclass
class SyncPlan:
    """What ``sync`` would do (JSON-serialisable)."""

    add: list[str] = field(default_factory=list)
    replace: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pdfs: dict[str, str] = field(default_factory=dict)  # ds -> pdf path


def plan_sync(conn: sqlite3.Connection, pdf_dir: Path, only: list[str] | None = None, force: bool = False) -> SyncPlan:
    """Compare the PDF folder with the DB by ``ds`` + ``edition`` + ``sha256``."""
    plan = SyncPlan()
    by_ds: dict[str, list[Path]] = {}
    name_re = C.pdf_name_re()
    for f in sorted(pdf_dir.glob("*.pdf")):
        m = name_re.match(f.name)
        if not m:
            plan.warnings.append(f"ignored (unexpected name): {f.name}")
            continue
        by_ds.setdefault(m.group(1), []).append(f)
    for ds, files in by_ds.items():
        files.sort(key=lambda p: p.stem.split("_")[1])
        if len(files) > 1:
            plan.warnings.append(
                f"{C.doc_label(ds)}: {len(files)} editions in folder ({', '.join(p.name for p in files)}); "
                f"indexing only the newest ({files[-1].name})"
            )
        by_ds[ds] = [files[-1]]
    in_db = {r["doc_id"]: dict(r) for r in conn.execute("SELECT doc_id, edition, source_sha256 FROM documents")}
    scope = set(only) if only else None
    for ds, (f,) in sorted(by_ds.items()):
        if scope is not None and ds not in scope:
            continue
        plan.pdfs[ds] = str(f)
        _, ed = parse_pdf_name(f)
        row = in_db.get(ds)
        if row is None:
            plan.add.append(ds)
        elif force or row["edition"] != ed or row["source_sha256"] != sha256_file(f):
            plan.replace.append(ds)
        else:
            plan.unchanged.append(ds)
    for ds in sorted(in_db):
        if ds not in by_ds and (scope is None or ds in scope):
            plan.remove.append(ds)
    return plan


@dataclass
class SyncResult:
    """Result of a sync run (JSON-serialisable)."""

    plan: SyncPlan
    results: list[ReplaceResult] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    healed: dict[str, list[ModelOutcome]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)  # ds -> error; the sheet keeps its previous state
    skipped: list[str] = field(default_factory=list)  # not touched because ``should_stop`` fired; previous state kept
    cancelled: bool = False  # ``should_stop`` fired: the run ended early, what finished stays finished
    report_path: str | None = None
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sync(
    conn: sqlite3.Connection,
    pdf_dir: Path,
    *,
    only: list[str] | None = None,
    text_models: list[str] | None = None,
    image_models: list[str] | None = None,
    embed: bool = True,
    dry_run: bool = False,
    force: bool = False,
    report_dir: Path | None = None,
    md_dir: Path | None = None,
    fig_dir: Path | None = None,
    cost_guard: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> SyncResult:
    """Bring the DB in line with ``pdf_dir`` (new/changed -> replace, vanished -> remove).

    Idempotent: a second run with an unchanged folder converts and embeds nothing (only missing
    embeddings of previously failed models are retried). ``cost_guard`` (USD) caps the embedding
    spend of this run; None keeps the library default ``MAX_TOTAL_COST_USD``.

    ``should_stop`` is polled only BETWEEN sheets (before each remove / replace / embedding repair), never inside a
    transaction: once it returns True the remaining sheets are listed in ``SyncResult.skipped`` and untouched.
    """
    from .report import write_sync_report

    t0 = time.perf_counter()
    text_models = C.TEXT_MODEL_KEYS if text_models is None else text_models
    image_models = C.IMAGE_MODEL_KEYS if image_models is None else image_models
    plan = plan_sync(conn, pdf_dir, only, force)
    for w in plan.warnings:  # e.g. a PDF whose name the pattern does not match is skipped: say so in the job log
        log(f"WARNING: {w}")
    res = SyncResult(plan=plan)
    if dry_run:
        return res
    embedders: dict[str, Embedder] = (
        {k: Embedder(C.MODELS[k], total_cost_guard=cost_guard) for k in {*text_models, *image_models}}
        if cost_guard is not None
        else {}
    )

    def stopped() -> bool:
        if not res.cancelled and should_stop is not None and should_stop():
            res.cancelled = True
            log("STOP requested: remaining sheets are left untouched")
        return res.cancelled

    for ds in plan.remove:
        if stopped():
            res.skipped.append(ds)
            continue
        if remove_document(conn, ds, md_dir, fig_dir):
            res.removed.append(ds)
            log(f"[{ds}] removed (PDF gone)")
    for ds in plan.add + plan.replace:
        if stopped():
            res.skipped.append(ds)
            continue
        try:
            r = replace_document(
                conn,
                Path(plan.pdfs[ds]),
                text_models=text_models,
                image_models=image_models,
                embedders=embedders,
                embed=embed,
                md_dir=md_dir,
                fig_dir=fig_dir,
                log=log,
            )
        except Exception as e:  # noqa: BLE001 - one sheet must not stop the batch; replace_document rolled it back
            res.failed[ds] = f"{type(e).__name__}: {str(e)[:300]}"
            log(f"[{ds}] FAILED, previous state kept: {res.failed[ds]}")
            continue
        res.results.append(r)
    if embed:
        for ds in plan.unchanged:
            missing = conn.execute(
                "SELECT model_key, kind FROM document_models WHERE doc_id=? AND status!='complete'", (ds,)
            ).fetchall()
            want_t = [m["model_key"] for m in missing if m["kind"] == "text" and m["model_key"] in text_models]
            want_i = [m["model_key"] for m in missing if m["kind"] == "image" and m["model_key"] in image_models]
            if not (want_t or want_i):
                continue
            if stopped():  # only sheets that really needed repair are reported as skipped
                res.skipped.append(ds)
                continue
            try:
                res.healed[ds] = heal_embeddings(conn, ds, want_t, want_i, embedders)
            except Exception as e:  # noqa: BLE001 - e.g. a figure file gone; the other sheets still heal
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                res.failed[ds] = (
                    f"embedding repair: {type(e).__name__}: {str(e)[:300]}; run sync --force for this sheet"
                )
                log(f"[{ds}] FAILED, previous state kept: {res.failed[ds]}")
    res.seconds = time.perf_counter() - t0
    if res.results or res.removed or res.failed:
        res.report_path = str(write_sync_report(res, report_dir or C.REPORT_DIR))
    return res


# ------------------------------------------------------------------------------ doctor
def doctor(
    conn: sqlite3.Connection, md_dir: Path | None = None, fig_dir: Path | None = None, pdf_dir: Path | None = None
) -> list[str]:
    """Integrity check; returns a list of problems (empty = healthy).

    With ``pdf_dir`` it also compares the DB with the PDF folder (unexpected names, PDFs not indexed).
    """
    md_dir = md_dir or C.MD_DIR
    fig_dir = fig_dir or C.FIG_DIR
    problems: list[str] = []
    sheets = {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM documents")}
    if pdf_dir is not None and pdf_dir.exists():
        for f in sorted(pdf_dir.glob("*.pdf")):
            m = C.pdf_name_re().match(f.name)
            if not m:
                problems.append(f"PDF {f.name}: unexpected file name, sync ignores it")
            elif m.group(1) not in sheets:
                problems.append(f"PDF {f.name}: not indexed")
    for ds, s in sheets.items():
        ed = s["edition"]
        n_u = conn.execute("SELECT COUNT(*) FROM units WHERE doc_id=?", (ds,)).fetchone()[0]
        if n_u != s["n_units"]:
            problems.append(f"{ds}: units rows {n_u} != documents.n_units {s['n_units']}")
        if not conn.execute("SELECT 1 FROM units WHERE doc_id=? AND kind='clause' LIMIT 1", (ds,)).fetchone():
            problems.append(f"{ds}: no numbered clause indexed (conversion lost the structure?)")
        n_f = conn.execute(
            "SELECT COUNT(*) FROM units_fts WHERE rowid IN (SELECT id FROM units WHERE doc_id=?)", (ds,)
        ).fetchone()[0]
        if n_f != n_u:
            problems.append(f"{ds}: FTS5 rows {n_f} != units {n_u}")
        n_c = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (ds,)).fetchone()[0]
        if n_c != s["n_chunks"]:
            problems.append(f"{ds}: chunks {n_c} != documents.n_chunks {s['n_chunks']}")
        for table in ("units", "chunks"):
            bad = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE doc_id=? AND edition!=?", (ds, ed)).fetchone()[0]
            if bad:
                problems.append(f"{ds}: {bad} rows in {table} carry another edition than {ed}")
        n_fig = conn.execute(
            "SELECT COUNT(*) FROM units WHERE doc_id=? AND kind='figure' AND extra LIKE '%\"file\"%'", (ds,)
        ).fetchone()[0]
        for m in conn.execute("SELECT * FROM document_models WHERE doc_id=?", (ds,)):
            key, kind = m["model_key"], m["kind"]
            if kind == "text":
                n_v = conn.execute(f"SELECT COUNT(*) FROM {vec_table(key)} WHERE doc_id=?", (ds,)).fetchone()[0]
                expect = n_c
            else:
                n_v = conn.execute(f"SELECT COUNT(*) FROM {figvec_table(key)} WHERE doc_id=?", (ds,)).fetchone()[0]
                expect = n_fig
            if m["status"] == "complete" and n_v != expect:
                problems.append(f"{ds}: model {key}/{kind} status complete but {n_v} vectors != {expect}")
            if n_v != m["n_ok"]:
                problems.append(f"{ds}: model {key}/{kind} n_ok {m['n_ok']} != vec rows {n_v}")
            if m["edition"] != ed:
                problems.append(f"{ds}: sheet_models edition {m['edition']} != {ed}")
        tag = edition_tag(ed)
        if not (md_dir / f"{ds}_{tag}.md").exists():
            problems.append(f"{ds}: Markdown file md/{ds}_{tag}.md missing")
        if n_fig and not (fig_dir / f"{ds}_{tag}").exists():
            problems.append(f"{ds}: figure folder figures/{ds}_{tag} missing")
        for r in conn.execute("SELECT unit_id, extra FROM units WHERE doc_id=? AND kind='figure'", (ds,)):
            f = json.loads(r["extra"] or "{}").get("file")
            if f and not (fig_dir / f"{ds}_{tag}" / f).exists():
                problems.append(f"{ds}: figure file {f} of {r['unit_id']} missing")
    # orphans
    q = {
        "units without sheet": "SELECT COUNT(*) FROM units WHERE doc_id NOT IN (SELECT doc_id FROM documents)",
        "chunks without unit": "SELECT COUNT(*) FROM chunks WHERE unit_row NOT IN (SELECT id FROM units)",
        "FTS rows without unit": "SELECT COUNT(*) FROM units_fts WHERE rowid NOT IN (SELECT id FROM units)",
        "document_models without sheet": "SELECT COUNT(*) FROM document_models WHERE doc_id NOT IN (SELECT doc_id FROM documents)",
    }
    for key in C.MODELS:
        q[f"{vec_table(key)} orphans"] = (
            f"SELECT COUNT(*) FROM {vec_table(key)} WHERE chunk_id NOT IN (SELECT id FROM chunks)"
        )
        q[f"{figvec_table(key)} orphans"] = (
            f"SELECT COUNT(*) FROM {figvec_table(key)} WHERE unit_row NOT IN (SELECT id FROM units)"
        )
    for name, sql in q.items():
        n = conn.execute(sql).fetchone()[0]
        if n:
            problems.append(f"orphans: {n} {name}")
    # files not matched to the DB
    known_md = {f"{ds}_{edition_tag(s['edition'])}.md" for ds, s in sheets.items()}
    for f in md_dir.glob("*.md") if md_dir.exists() else []:
        if f.name not in known_md:
            problems.append(f"md/{f.name} is not in the DB (stale edition?)")
    known_fig = {f"{ds}_{edition_tag(s['edition'])}" for ds, s in sheets.items()}
    for d in fig_dir.iterdir() if fig_dir.exists() else []:
        if d.is_dir() and d.name not in known_fig:
            problems.append(f"figures/{d.name} is not in the DB (stale edition?)")
    return problems
