"""DB tests: replace removes the old edition everywhere, sync idempotency, diff/renumbering, doctor."""

from __future__ import annotations

import pytest

from tests.helpers import BODY_V1, BODY_V2, fake_converter, fake_embedders
from vedun import config as C
from vedun import db as D
from vedun.check_citations import extract_citations, validate
from vedun.search import figure, get_section, list_documents, lookup, rank_of, search

TM, IM = ["t1", "i1"], ["i1"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-real-key")
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    dirs = {"md": tmp_path / "md", "fig": tmp_path / "figures"}
    monkeypatch.setattr(C, "MD_DIR", dirs["md"])
    monkeypatch.setattr(C, "FIG_DIR", dirs["fig"])
    conn = D.connect(tmp_path / "t.db")
    return conn, pdf_dir, dirs, tmp_path


def _put_pdf(pdf_dir, name, content=b"%PDF-fake"):
    p = pdf_dir / name
    p.write_bytes(content)
    return p


def _replace(conn, pdf, dirs, bodies, embed=True):
    return D.replace_document(
        conn,
        pdf,
        text_models=TM,
        image_models=IM,
        md_dir=dirs["md"],
        fig_dir=dirs["fig"],
        embedders=fake_embedders(["t1", "i1"]),
        embed=embed,
        converter=fake_converter(bodies),
    )


def _row_counts(conn, ds, edition):
    """Count rows of (ds, edition) in every table that stores them."""
    n = {}
    for t in ("documents", "units", "chunks", "document_models", "units_fts"):
        if t == "units_fts":
            n[t] = conn.execute(
                "SELECT COUNT(*) FROM units_fts WHERE doc_id=? AND edition=?", (ds, edition)
            ).fetchone()[0]
        else:
            n[t] = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE doc_id=? AND edition=?", (ds, edition)).fetchone()[0]
    for k in C.MODELS:
        n[D.vec_table(k)] = conn.execute(
            f"SELECT COUNT(*) FROM {D.vec_table(k)} WHERE doc_id=? AND edition=?", (ds, edition)
        ).fetchone()[0]
        n[D.figvec_table(k)] = conn.execute(
            f"SELECT COUNT(*) FROM {D.figvec_table(k)} WHERE doc_id=? AND edition=?", (ds, edition)
        ).fetchone()[0]
    return n


def test_replace_leaves_no_row_of_old_edition(env):
    conn, pdf_dir, dirs, _ = env
    bodies = {"2026-10": BODY_V1, "2027-04": BODY_V2}
    other = _put_pdf(pdf_dir, "9-1_202601.pdf", b"other")
    _replace(conn, other, dirs, {"2026-01": BODY_V1})  # a second sheet that must survive
    old = _put_pdf(pdf_dir, "9-9_202610.pdf", b"old")
    r1 = _replace(conn, old, dirs, bodies)
    before = _row_counts(conn, "9-9", "2026-10")
    populated = ("documents", "units", "chunks", "document_models", "units_fts", "vec_t1", "vec_i1", "figvec_i1")
    assert all(before[k] > 0 for k in populated), before
    assert (dirs["md"] / "9-9_202610.md").exists() and (dirs["fig"] / "9-9_202610").exists()
    other_before = _row_counts(conn, "9-1", "2026-01")

    old.unlink()
    new = _put_pdf(pdf_dir, "9-9_202704.pdf", b"new")
    r2 = _replace(conn, new, dirs, bodies)
    assert r1.action == "added" and r2.action == "replaced" and r2.old_edition == "2026-10"

    after_old = _row_counts(conn, "9-9", "2026-10")
    assert all(v == 0 for v in after_old.values()), after_old  # nothing of the old edition anywhere
    after_new = _row_counts(conn, "9-9", "2027-04")
    assert after_new["documents"] == 1 and after_new["units"] > 0 and after_new["vec_t1"] > 0
    assert _row_counts(conn, "9-1", "2026-01") == other_before  # other sheets untouched
    # files: old md + figure folder gone, new present
    assert not (dirs["md"] / "9-9_202610.md").exists() and not (dirs["fig"] / "9-9_202610").exists()
    assert (dirs["md"] / "9-9_202704.md").exists() and (dirs["fig"] / "9-9_202704").exists()
    # vec0 shadow rowids of the old edition really are gone: total vectors = both sheets' chunks
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM vec_t1_rowids").fetchone()[0] == total_chunks
    assert (
        conn.execute("SELECT COUNT(*) FROM units_fts").fetchone()[0]
        == conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    )
    assert D.doctor(conn, dirs["md"], dirs["fig"]) == []
    # the edition report caught the renumbering and the textual edit
    d = r2.diff
    assert d is not None
    ren = {r.old_id: r.new_id for r in d.renumbered}
    assert ren.get("2.3.2.1") == "2.3.4.1.1"
    assert ren.get("Table 9a") == "Table 2.11.1.1"
    assert "2.3.2.2" in d.removed and "2.3.4.2" in d.added
    assert any(c.unit_id == "2.3.2.4" and "[-45-]" in c.diff for c in d.changed)
    assert "Renumbered clauses" in d.changes_section
    # old citation now resolves to a suggestion
    res = validate(conn, extract_citations("ASC 9-9 pkt 2.3.2.1 and ASC 9-9 Tab. 9a"))
    assert [r.status for r in res] == ["UNKNOWN UNIT", "UNKNOWN UNIT"]
    assert "2.3.4.1.1" in res[0].suggestion and "Table 2.11.1.1" in res[1].suggestion


def test_plan_sync_accepts_letter_suffix_sheets(env):
    conn, pdf_dir, _dirs, _tmp = env
    for name in ("7-111A_202610.pdf", "4-1N_202504.pdf", "9-9_202610.pdf"):
        _put_pdf(pdf_dir, name)
    plan = D.plan_sync(conn, pdf_dir)
    assert sorted(plan.add) == ["4-1N", "7-111A", "9-9"] and not plan.warnings


def test_writer_connection_has_large_page_cache(env):
    conn = env[0]
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -131072  # no early spill = readers not blocked


def test_sync_idempotent_remove_and_duplicate_editions(env):
    conn, pdf_dir, dirs, tmp = env
    _put_pdf(pdf_dir, "9-9_202610.pdf", b"a")
    _put_pdf(pdf_dir, "9-9_202704.pdf", b"b")  # two editions of the same ds in the folder
    plan = D.plan_sync(conn, pdf_dir)
    assert plan.add == ["9-9"] and any("2 editions" in w for w in plan.warnings)
    assert plan.pdfs["9-9"].endswith("9-9_202704.pdf")  # newest wins
    (pdf_dir / "9-9_202610.pdf").unlink()
    bodies = {"2026-10": BODY_V1, "2027-04": BODY_V2}

    def run():
        # monkey-patch the converter used inside sync via replace_document default
        import vedun.db as dbm

        orig = dbm.replace_document

        def patched(*a, **k):
            k.setdefault("converter", fake_converter(bodies))
            k["md_dir"], k["fig_dir"] = dirs["md"], dirs["fig"]
            k["embedders"] = fake_embedders(["t1", "i1"])
            return orig(*a, **k)

        dbm.replace_document = patched
        try:
            return D.sync(
                conn,
                pdf_dir,
                text_models=TM,
                image_models=IM,
                report_dir=tmp / "reports",
                md_dir=dirs["md"],
                fig_dir=dirs["fig"],
            )
        finally:
            dbm.replace_document = orig

    r1 = run()
    assert r1.plan.add == ["9-9"] and len(r1.results) == 1
    r2 = run()  # idempotent: nothing to do
    assert r2.plan.unchanged == ["9-9"] and not r2.results and not r2.removed and r2.report_path is None
    # PDF content changed (same name) -> replaced
    (pdf_dir / "9-9_202704.pdf").write_bytes(b"changed")
    assert D.plan_sync(conn, pdf_dir).replace == ["9-9"]
    # PDF vanished -> removed everywhere
    (pdf_dir / "9-9_202704.pdf").unlink()
    r3 = run()
    assert r3.removed == ["9-9"] and r3.report_path
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vec_t1_rowids").fetchone()[0] == 0
    assert not list(dirs["md"].glob("*.md")) and not list(dirs["fig"].glob("*"))
    assert D.doctor(conn, dirs["md"], dirs["fig"]) == []


def test_sync_continues_after_one_sheet_fails(env, monkeypatch):
    conn, pdf_dir, dirs, tmp = env
    _put_pdf(pdf_dir, "9-8_202610.pdf", b"a")
    _put_pdf(pdf_dir, "9-9_202610.pdf", b"b")
    orig = D.replace_document

    def flaky(conn_, pdf, **k):
        if pdf.name.startswith("9-8"):  # e.g. a transient I/O error on a bind mount
            raise FileNotFoundError("staging/9-8/figures/fig_p001_01.png")
        k.update(converter=fake_converter({"2026-10": BODY_V1}), embedders=fake_embedders(["t1", "i1"]))
        return orig(conn_, pdf, **k)

    monkeypatch.setattr(D, "replace_document", flaky)
    lines: list[str] = []
    res = D.sync(
        conn,
        pdf_dir,
        text_models=TM,
        image_models=IM,
        report_dir=tmp / "reports",
        md_dir=dirs["md"],
        fig_dir=dirs["fig"],
        log=lines.append,
    )
    assert [r.doc_id for r in res.results] == ["9-9"]
    assert list(res.failed) == ["9-8"] and "FileNotFoundError" in res.failed["9-8"]
    assert any("[9-8] FAILED" in s for s in lines)
    assert "9-8: FAILED" in (tmp / "reports").joinpath(res.report_path).read_text(encoding="utf-8")
    assert D.plan_sync(conn, pdf_dir).add == ["9-8"]  # the next sync picks it up again


def test_lookup_search_section_figure(env):
    conn, pdf_dir, dirs, _ = env
    pdf = _put_pdf(pdf_dir, "9-9_202610.pdf")
    _replace(conn, pdf, dirs, {"2026-10": BODY_V1})
    r = lookup(conn, "9-9", "pkt 2.3.2.4")
    e = r.first
    assert r.found and e and "45 kg (99 lb)" in e.text and e.unit_id == "2.3.2.4"
    assert e.citation_pl == "ASC 9-9 „Test Sheet” (April 2019, Amended October 2026), pkt 2.3.2.4"
    assert e.citation_en.endswith("Section 2.3.2.4") and "p." not in e.citation_en
    assert e.source_pdf and e.source_pdf.endswith("9-9_202610.pdf") and e.source_sha256
    assert e.md_line_start and e.md_line_end >= e.md_line_start and e.md_path.endswith("9-9_202610.md")
    assert e.source_item_id is None and "source_item_id" in e.null_reasons  # unknown -> explicit null + reason
    assert [p["unit_id"] for p in e.parent_path] == ["1.0", "2.0", "2.3"][1:]  # existing ancestors only
    t = lookup(conn, "9-9", "tab. 9a").first
    assert t and t.unit_kind == "table" and t.citation_pl.endswith("Tab. 9a")
    miss = lookup(conn, "9-9", "2.3.2.9").items[0]
    assert not miss.found and "2.3.2.4" in miss.nearest  # explicit not-found + nearest, no guess
    assert not lookup(conn, "1-1", "2.1").document_indexed
    sec = get_section(conn, "9-9", "2.3", include_children=True)
    assert sec.found and "45 kg" in sec.text and "semiannually" in sec.text
    assert "45 kg" not in get_section(conn, "9-9", "2.3", include_children=False).text
    nb = lookup(conn, "9-9", "2.3.2.2", neighbors=True).items[0]
    assert (nb.prev_id, nb.next_id) == ("2.3.2.1", "2.3.2.3")
    ch = lookup(conn, "9-9", "2.3.2", include_children=False)  # numbering gap: 2.3.2 has no unit
    assert not ch.found and "2.3.2.1" in ch.items[0].nearest
    hits = search(conn, "fire door hinges semiannually", mode="bm25", include_figures=False).hits
    assert rank_of(hits, "9-9", "Table 9a") == 1 and "**" in (hits[0].snippet or "")
    fac = lambda k: fake_embedders([k])[k]  # noqa: E731
    hv = search(conn, "fire door hinges", rerank=None, embedder_factory=fac)
    assert hv.hits and all(h.doc_id == "9-9" for h in hv.hits)
    h0 = hv.hits[0].retrieval
    assert h0.fusion == "wrrf" and (h0.w_fts, h0.w_vec) == (0.4, 0.6) and h0.rrf and h0.match_type == "search"
    ex = search(conn, "co mowi ASC 9-9 pkt 2.3.2.4 o cieczach", rerank=None, embedder_factory=fac).hits[0]
    assert ex.unit_id == "2.3.2.4" and ex.retrieval.match_type == "exact" and ex.retrieval.final_rank == 1
    fig = figure(conn, "9-9", "Fig. 2.3.1")
    assert fig.ok and fig.path and fig.path.endswith("fig_p003_01.png") and fig.evidence.figure_png == fig.path
    assert fig.evidence.citation_pl.endswith("rys. 2.3.1") and fig.evidence.citation_en.endswith("Fig. 2.3.1")
    assert list_documents(conn)[0].edition == "2026-10"


def test_doctor_flags_stale_files_and_orphans(env):
    conn, pdf_dir, dirs, _ = env
    pdf = _put_pdf(pdf_dir, "9-9_202610.pdf")
    _replace(conn, pdf, dirs, {"2026-10": BODY_V1})
    assert D.doctor(conn, dirs["md"], dirs["fig"]) == []
    (dirs["md"] / "9-9_202001.md").write_text("old", encoding="utf-8")
    (dirs["fig"] / "9-9_202001").mkdir()
    conn.execute("DELETE FROM vec_t1 WHERE chunk_id = (SELECT MIN(chunk_id) FROM vec_t1)")
    probs = D.doctor(conn, dirs["md"], dirs["fig"])
    assert any("9-9_202001.md" in p for p in probs)
    assert any("figures/9-9_202001" in p for p in probs)
    assert any("vec_t1" in p or "t1/text" in p for p in probs)


def test_doctor_compares_db_with_pdf_folder(env):
    conn, pdf_dir, dirs, _tmp = env
    _put_pdf(pdf_dir, "7-111A_202610.pdf")
    _put_pdf(pdf_dir, "ASC 3-26 copy.pdf")
    probs = D.doctor(conn, dirs["md"], dirs["fig"], pdf_dir=pdf_dir)
    assert "PDF 7-111A_202610.pdf: not indexed" in probs
    assert "PDF ASC 3-26 copy.pdf: unexpected file name, sync ignores it" in probs


def test_reuse_converted_copies_figures_whose_caption_has_brackets(env, monkeypatch):
    """Alt text like "[ACMEORG]" broke the copy regex: the figure was not staged and the image embed hit ENOENT."""
    import hashlib

    from vedun import convert

    conn, pdf_dir, dirs, tmp = env
    pdf = _put_pdf(pdf_dir, "9-9_202610.pdf")
    monkeypatch.setattr(convert, "edition_label", lambda p, e: ("October 2026", "printed on the sheet"))
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (dirs["md"]).mkdir(parents=True)
    (dirs["fig"] / "9-9_202610").mkdir(parents=True)
    for n in ("fig_p001_01.png", "fig_p001_02.png", "fig_p001_99.png"):
        (dirs["fig"] / "9-9_202610" / n).write_bytes(b"png")
    (dirs["md"] / "9-9_202610.md").write_text(
        f"---\nds: 9-9\nformat: {convert.CONVERT_FORMAT}\nsource_sha256: {sha}\nedition_label: October 2026\n---\n\n"
        "![Fig. 1. Joints (courtesy of [ACMEORG].)](../figures/9-9_202610/fig_p001_01.png)\n\n"
        "![Fig. 2. Main, 8 in. [20.3 cm] diameter](../figures/9-9_202610/fig_p001_02.png)\n",
        encoding="utf-8",
    )
    info = D.reuse_converted(pdf, dirs["md"], dirs["fig"], tmp / "stage")
    assert info is not None
    assert sorted(p.name for p in info.fig_dir.iterdir()) == ["fig_p001_01.png", "fig_p001_02.png"]  # no stale 99
    assert info.n_figures == 2


def test_reuse_converted_refuses_when_a_figure_file_is_gone(env, monkeypatch):
    """A missing source figure means reconvert; reusing would commit the sheet with that figure deleted."""
    import hashlib

    from vedun import convert

    conn, pdf_dir, dirs, tmp = env
    pdf = _put_pdf(pdf_dir, "9-9_202610.pdf")
    monkeypatch.setattr(convert, "edition_label", lambda p, e: ("October 2026", "printed on the sheet"))
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    dirs["md"].mkdir(parents=True)
    (dirs["fig"] / "9-9_202610").mkdir(parents=True)
    (dirs["fig"] / "9-9_202610" / "fig_p001_01.png").write_bytes(b"png")
    (dirs["md"] / "9-9_202610.md").write_text(
        f"---\nds: 9-9\nformat: {convert.CONVERT_FORMAT}\nsource_sha256: {sha}\nedition_label: October 2026\n---\n\n"
        "![Fig. 1. Here](../figures/9-9_202610/fig_p001_01.png)\n\n"
        "![Fig. 2. Gone](../figures/9-9_202610/fig_p001_02.png)\n",
        encoding="utf-8",
    )
    assert D.reuse_converted(pdf, dirs["md"], dirs["fig"], tmp / "stage") is None
    assert not (tmp / "stage").exists()


# ------------------------------------------------------------------ cooperative stop (should_stop)
def _sync_kw(pdf_dir, dirs, tmp, **extra):
    return dict(
        text_models=TM,
        image_models=IM,
        report_dir=tmp / "reports",
        md_dir=dirs["md"],
        fig_dir=dirs["fig"],
        **extra,
    )


def _patched_replace(monkeypatch, after_call=None, fail_for=()):
    """``D.replace_document`` with the fakes injected; ``after_call(ds)`` runs once a sheet is committed."""
    orig = D.replace_document

    def wrapped(conn_, pdf, **k):
        if pdf.name.split("_")[0] in fail_for:
            raise FileNotFoundError("staging vanished")
        k.update(converter=fake_converter({"2026-10": BODY_V1}), embedders=fake_embedders(["t1", "i1"]))
        r = orig(conn_, pdf, **k)
        if after_call:
            after_call(r.doc_id)
        return r

    monkeypatch.setattr(D, "replace_document", wrapped)


def test_sync_stops_between_sheets_and_lists_the_skipped(env, monkeypatch):
    conn, pdf_dir, dirs, tmp = env
    for ds in ("9-7", "9-8", "9-9"):
        _put_pdf(pdf_dir, f"{ds}_202610.pdf", ds.encode())
    stop = {"now": False}
    _patched_replace(monkeypatch, after_call=lambda ds: stop.update(now=True))  # the first sheet commits, then STOP
    lines: list[str] = []
    res = D.sync(conn, pdf_dir, should_stop=lambda: stop["now"], log=lines.append, **_sync_kw(pdf_dir, dirs, tmp))
    assert res.cancelled and [r.doc_id for r in res.results] == ["9-7"] and res.skipped == ["9-8", "9-9"]
    assert [r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents")] == [
        "9-7"
    ]  # the finished sheet stays finished
    assert D.doctor(conn, dirs["md"], dirs["fig"]) == []  # nothing half-written
    assert any("STOP requested" in s for s in lines)
    assert "9-7" in (tmp / "reports").joinpath(res.report_path).read_text(encoding="utf-8")
    again = D.sync(conn, pdf_dir, **_sync_kw(pdf_dir, dirs, tmp))  # a later sync simply continues
    assert not again.cancelled and again.skipped == [] and sorted(r.doc_id for r in again.results) == ["9-8", "9-9"]


def test_sync_stop_keeps_per_sheet_failure_isolation(env, monkeypatch):
    conn, pdf_dir, dirs, tmp = env
    for ds in ("9-7", "9-8", "9-9"):
        _put_pdf(pdf_dir, f"{ds}_202610.pdf", ds.encode())
    stop = {"now": False}
    # 9-7 fails (recorded, previous state kept), 9-8 commits and triggers the stop, 9-9 is skipped
    _patched_replace(monkeypatch, after_call=lambda ds: stop.update(now=True), fail_for=("9-7",))
    res = D.sync(conn, pdf_dir, should_stop=lambda: stop["now"], **_sync_kw(pdf_dir, dirs, tmp))
    assert list(res.failed) == ["9-7"] and [r.doc_id for r in res.results] == ["9-8"] and res.skipped == ["9-9"]
    assert res.cancelled


def test_sync_stop_before_the_first_sheet_changes_nothing_and_skips_removals_and_repairs(env, monkeypatch):
    conn, pdf_dir, dirs, tmp = env
    pdf = _put_pdf(pdf_dir, "9-9_202610.pdf", b"b")
    _replace(conn, pdf, dirs, {"2026-10": BODY_V1}, embed=False)  # indexed, embeddings pending -> needs repair
    other = _put_pdf(pdf_dir, "9-8_202610.pdf", b"a")
    _replace(conn, other, dirs, {"2026-10": BODY_V1})
    other.unlink()  # 9-8: PDF gone -> a removal is planned
    _put_pdf(pdf_dir, "9-7_202610.pdf", b"c")  # 9-7: new -> an add is planned
    _patched_replace(monkeypatch)
    res = D.sync(conn, pdf_dir, should_stop=lambda: True, **_sync_kw(pdf_dir, dirs, tmp))
    assert res.cancelled and not res.results and not res.removed and not res.healed
    assert sorted(res.skipped) == ["9-7", "9-8", "9-9"]  # add, removal and repair are all left for the next run
    assert sorted(r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents")) == ["9-8", "9-9"]
    assert res.report_path is None  # nothing happened, no report


def test_sync_without_should_stop_is_unchanged(env, monkeypatch):
    conn, pdf_dir, dirs, tmp = env
    _put_pdf(pdf_dir, "9-9_202610.pdf", b"b")
    _patched_replace(monkeypatch)
    res = D.sync(conn, pdf_dir, **_sync_kw(pdf_dir, dirs, tmp))
    assert not res.cancelled and res.skipped == [] and [r.doc_id for r in res.results] == ["9-9"]


def test_sync_report_lists_the_sheets_a_cancel_skipped(env, monkeypatch):
    conn, pdf_dir, dirs, tmp = env
    _put_pdf(pdf_dir, "9-8_202610.pdf", b"a")
    _put_pdf(pdf_dir, "9-9_202610.pdf", b"b")
    done: list[str] = []
    _patched_replace(monkeypatch, after_call=done.append)
    res = D.sync(conn, pdf_dir, should_stop=lambda: bool(done), **_sync_kw(pdf_dir, dirs, tmp))  # stop after one
    assert res.cancelled and [r.doc_id for r in res.results] == ["9-8"] and res.skipped == ["9-9"]
    text = open(res.report_path, encoding="utf-8").read()
    assert "Cancelled: 1 document(s) not processed" in text and "9-9" in text


_V3_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sheets(ds TEXT PRIMARY KEY, edition TEXT NOT NULL, title TEXT, source_pdf TEXT, source_sha256 TEXT,
    md_file TEXT, fig_dir TEXT, pages INTEGER, converter TEXT, convert_seconds REAL, seconds_per_page REAL,
    converted_at TEXT, indexed_at TEXT, n_units INTEGER, n_chunks INTEGER, n_figures INTEGER, edition_label TEXT,
    edition_label_source TEXT, source_path TEXT, fm_item_id TEXT);
CREATE TABLE sheet_models(ds TEXT NOT NULL, edition TEXT NOT NULL, model_key TEXT NOT NULL, kind TEXT NOT NULL,
    model_id TEXT, dim INTEGER, status TEXT, n_expected INTEGER, n_ok INTEGER, error TEXT,
    PRIMARY KEY(ds, model_key, kind));
CREATE TABLE units(id INTEGER PRIMARY KEY, ds TEXT NOT NULL, edition TEXT NOT NULL, unit_id TEXT NOT NULL,
    dup_index INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL, parent TEXT, title TEXT, text TEXT, page_start INTEGER,
    page_end INTEGER, seq INTEGER, extra TEXT, md_line_start INTEGER, md_line_end INTEGER,
    UNIQUE(ds, unit_id, dup_index));
CREATE INDEX units_ds ON units(ds);
CREATE INDEX units_parent ON units(ds, parent);
CREATE TABLE chunks(id INTEGER PRIMARY KEY, ds TEXT NOT NULL, edition TEXT NOT NULL, unit_row INTEGER NOT NULL,
    chunk_no INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE renames(ds TEXT NOT NULL, old_edition TEXT, new_edition TEXT NOT NULL, old_id TEXT NOT NULL,
    new_id TEXT NOT NULL, score REAL, PRIMARY KEY(ds, new_edition, old_id));
CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(title, text, uid, ds UNINDEXED, edition UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2');
INSERT INTO meta VALUES('schema_version', '3');
INSERT INTO sheets(ds, edition, title, fm_item_id) VALUES('9-9', '2026-10', 'Paint', 'GUID-1');
INSERT INTO sheet_models VALUES('9-9', '2026-10', 'i1', 'text', 'x', 4, 'complete', 1, 1, NULL);
INSERT INTO units(id, ds, edition, unit_id, kind, text) VALUES(7, '9-9', '2026-10', '2.3.2.4', 'clause', 'Limit paint');
INSERT INTO chunks(id, ds, edition, unit_row, chunk_no, text) VALUES(11, '9-9', '2026-10', 7, 0, 'Limit paint');
INSERT INTO renames VALUES('9-9', '2026-04', '2026-10', '2.3.2.3', '2.3.2.4', 0.9);
INSERT INTO units_fts(rowid, title, text, uid, ds, edition) VALUES(7, '', 'Limit paint', '2.3.2.4', '9-9', '2026-10');
"""


def test_migrate_schema_3_keeps_every_row_and_vector(tmp_path):
    import sqlite3

    import sqlite_vec

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.enable_load_extension(True)
    sqlite_vec.load(raw)
    raw.executescript(_V3_DDL)
    for key, spec in C.MODELS.items():
        for table, pk in ((D.vec_table(key), "chunk_id"), (D.figvec_table(key), "unit_row")):
            raw.execute(
                f"CREATE VIRTUAL TABLE {table} USING vec0({pk} integer primary key, ds text, edition text, "
                f"embedding float[{spec.dim}] distance_metric=cosine)"
            )
    vec = sqlite_vec.serialize_float32([1.0] + [0.0] * (C.MODELS["i1"].dim - 1))
    raw.execute("INSERT INTO vec_i1(chunk_id, ds, edition, embedding) VALUES(11, '9-9', '2026-10', ?)", (vec,))
    raw.commit()
    raw.close()

    conn = D.connect(path)  # migrates on open
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"documents", "document_models"} <= names and not {"sheets", "sheet_models"} & names
    assert not any(n.endswith("__v4") for n in names)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == D.SCHEMA_VERSION
    doc = conn.execute("SELECT doc_id, title, source_item_id FROM documents").fetchone()
    assert tuple(doc) == ("9-9", "Paint", "GUID-1")
    for table in ("document_models", "units", "chunks", "renames"):
        assert conn.execute(f"SELECT doc_id FROM {table}").fetchone()[0] == "9-9", table
    fts = conn.execute("SELECT rowid, doc_id FROM units_fts WHERE units_fts MATCH 'paint'").fetchall()
    assert [tuple(r) for r in fts] == [(7, "9-9")]
    hit = conn.execute(
        "SELECT chunk_id, distance FROM vec_i1 WHERE embedding MATCH ? AND k = 1 AND doc_id = ?", (vec, "9-9")
    ).fetchone()
    assert hit[0] == 11 and hit[1] < 1e-6
    assert D.migrate(conn) is False  # idempotent
    conn.close()
