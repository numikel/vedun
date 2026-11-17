"""API tests: forgiving refs, fulltext (phrase / NOT / NEAR / no vectors / bad syntax), toc, verify_quote, wRRF."""

from __future__ import annotations

import pytest

from tests.helpers import BODY_V1, fake_converter, fake_embedders, make_pdf
from vedun import db as D
from vedun.embed import Embedder
from vedun.quote import match_key, verify_quote
from vedun.search import fulltext, parse_ref, route_query, toc, weighted_rrf


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    from vedun import config as C

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(C, "MD_DIR", tmp_path / "md")
    monkeypatch.setattr(C, "FIG_DIR", tmp_path / "figures")
    conn = D.connect(tmp_path / "t.db")
    lines = [(50, 80, "2.3.2.4 Limit paint stock to 45 kg (99 lb) per cabinet.")]
    lines += [(50, 100, "Inspect fire door hinges semi-"), (50, 112, "annually.")]
    pdf = make_pdf(tmp_path / "9-9_202610.pdf", lines)
    dirs = {"md": tmp_path / "md", "fig": tmp_path / "figures"}
    D.replace_document(
        conn,
        pdf,
        text_models=["t1"],
        image_models=["i1"],
        md_dir=dirs["md"],
        fig_dir=dirs["fig"],
        embedders=fake_embedders(["t1", "i1"]),
        converter=fake_converter({"2026-10": BODY_V1}),
    )
    return conn


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.3.7.1.6", "2.3.7.1.6"),
        ("pkt 2.3.7.1.6", "2.3.7.1.6"),
        ("Pkt. 2.3.7.1.6.", "2.3.7.1.6"),
        ("punkt 2.3.7.1.6", "2.3.7.1.6"),
        ("Section 2.3.7.1.6", "2.3.7.1.6"),
        ("clause 2.3.7.1.6", "2.3.7.1.6"),
        ("Tab. 2.15.1.1", "Table 2.15.1.1"),
        ("Table 2.15.1.1", "Table 2.15.1.1"),
        ("tabela 2.15.1.1", "Table 2.15.1.1"),
        ("Fig. 2.4.3.1.1.1", "Figure 2.4.3.1.1.1"),
        ("rys. 2.4.3.1.1.1", "Figure 2.4.3.1.1.1"),
        ("Figure 3.3.3.4.A", "Figure 3.3.3.4.A"),
        ("zał. A", "Appendix A"),
        ("Appendix B", "Appendix B"),
        ("Table C-1", "Table C-1"),
    ],
)
def test_forgiving_ref_parsing(raw, expected):
    assert parse_ref(raw) == expected


def test_route_query_finds_sheet_and_ids():
    assert route_query("co mowi ASC 21-4 pkt 2.3.7.1.6") == (["21-4"], ["2.3.7.1.6"])
    assert route_query("ASC 22-6 Tab. 2.15.1.1 i Table 2.15.2.1") == (["22-6"], ["Table 2.15.1.1", "Table 2.15.2.1"])
    assert route_query("limit cieczy palnych 1.5 m od 10,800 ft2") == ([], [])  # numbers are not ids


def test_weighted_rrf_formula_and_missing_list():
    s = weighted_rrf([(["a", "b"], 0.6), (["b", "c"], 0.4)], k=60)
    assert s["a"] == pytest.approx(0.6 / 61)  # absent from list 2 -> contributes 0
    assert s["b"] == pytest.approx(0.6 / 62 + 0.4 / 61)
    assert s["c"] == pytest.approx(0.4 / 62)


def test_fulltext_phrase_not_near_and_prefix(kb):
    ph = fulltext(kb, '"fire door hinges"', syntax="fts5")
    assert ph.ok and [h.unit_id for h in ph.hits] == ["Table 9a"]
    assert "**" in (ph.hits[0].snippet or "")
    both = fulltext(kb, "paint OR lights", syntax="fts5")
    assert {h.unit_id for h in both.hits} >= {"2.3.2.4", "2.3.2.2"}
    notq = fulltext(kb, "paint OR lights NOT lights", syntax="fts5")
    assert {h.unit_id for h in notq.hits} == {"2.3.2.4"}
    near = fulltext(kb, 'NEAR("door" "semiannually", 5)', syntax="fts5")
    assert near.ok and [h.unit_id for h in near.hits] == ["Table 9a"]
    far = fulltext(kb, 'NEAR("door" "annually", 0)', syntax="fts5")
    assert far.ok and far.hits == []
    pre = fulltext(kb, "hing*", syntax="fts5")
    assert pre.ok and pre.hits
    safe = fulltext(kb, "inspect hinges", syntax="safe")  # AND of plain terms
    assert [h.unit_id for h in safe.hits] == ["Table 9a"]
    page = fulltext(kb, "the OR of", syntax="safe", limit=1, offset=0)
    assert len(page.hits) <= 1


def test_fulltext_malformed_fts5_returns_error_not_exception(kb):
    for bad in ['"unterminated', "AND AND", "NEAR(", "foo AND", "col:::x", "(((("]:
        r = fulltext(kb, bad, syntax="fts5")
        assert r.ok is False and r.error and r.error.startswith(("invalid FTS5 expression", "empty query")), bad
        assert r.hits == []
    assert fulltext(kb, "", syntax="safe").ok is False
    assert fulltext(kb, '"unterminated', syntax="safe").ok in (True, False)  # safe mode never raises


def test_fulltext_never_touches_vectors_or_network(kb, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fulltext must not call the embeddings API")

    monkeypatch.setattr(Embedder, "_request", boom)
    monkeypatch.setattr(Embedder, "embed_texts", boom)
    r = fulltext(kb, "paint", ds="9-9", kind="clause")
    assert r.ok and r.hits and all(h.unit_kind == "clause" for h in r.hits)
    assert fulltext(kb, "paint", kind="table").hits == []


def test_toc_branch_and_depth(kb):
    full = toc(kb, "9-9")
    ids = [i.unit_id for i in full.items]
    assert ids[:2] == ["1.0", "1.2"] and "Table 9a" in ids and full.found
    br = toc(kb, "9-9", "2.3", depth=1)
    assert [i.unit_id for i in br.items][0] == "2.3"
    assert all(i.depth <= 1 for i in br.items)
    assert "text" not in br.items[0].to_dict()  # light records
    assert not toc(kb, "9-9", "7.7").found
    assert not toc(kb, "1-1").found


def test_verify_quote_unit_and_altered(kb):
    ok = verify_quote(kb, "9-9", "pkt 2.3.2.4", "Limit paint stock to 45 kg (99 lb) per cabinet")
    assert ok.in_unit and ok.in_pdf and ok.match and ok.pdf_pages == [1]
    bad = verify_quote(kb, "9-9", "2.3.2.4", "Limit paint stock to 45 kg (90 lb) per cabinet")
    assert not bad.in_unit and bad.in_pdf is False and not bad.match and bad.closest and bad.closest_ratio > 0.8
    assert bad.closest_unit_id == "2.3.2.4"
    other = verify_quote(kb, "9-9", "2.3.2.2", "Limit paint stock to 45 kg (99 lb) per cabinet")
    assert not other.in_unit and "2.3.2.4" in other.found_in_units  # tells which unit really contains it
    assert match_key("fire-\nprotection ft 2") == match_key("fireprotection ft2")


def test_bm25_noise_gate_drops_cross_language_matches(kb):
    from vedun.search import bm25_ranked, coverage_gate, sanitize_fts, significant_terms

    assert significant_terms("limit cieczy palnych w strefie KR-2") == ["limit", "cieczy", "palnych", "strefie", "kr-2"]
    pl = "ładowanie wózków w wydzielonym pomieszczeniu paint"  # Polish words + one stray English hit
    raw = bm25_ranked(kb, sanitize_fts(pl, drop_stop=True), 20)
    assert raw and coverage_gate(kb, pl, raw) == []  # one stray word out of five is not evidence
    en = "fire door hinges inspect semiannually"
    kept = coverage_gate(kb, en, bm25_ranked(kb, sanitize_fts(en, drop_stop=True), 20))
    assert kept  # genuine English matches survive
    assert sanitize_fts("the of and w na", drop_stop=True)  # only stop words: falls back to all words
