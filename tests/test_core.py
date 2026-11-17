"""Unit tests: clause ids, FTS5 sanitising, RRF, citation regex, chunking, units parsing."""

from __future__ import annotations

import sqlite3

import pytest

from tests.helpers import BODY_V1, make_md
from vedun.check_citations import extract_citations
from vedun.db import split_table, split_text
from vedun.search import rrf, sanitize_fts
from vedun.units import (
    canonical_clause,
    canonical_figure,
    canonical_table,
    clause_depth,
    parent_id,
    parse_markdown,
    sort_key,
)


def test_clause_ids():
    assert parent_id("2.3.7.1.6") == "2.3.7.1"
    assert parent_id("2.4") == "2.0"
    assert parent_id("2.0") is None
    assert parent_id("Table 2.4.1.4.1") is None
    assert clause_depth("1.0") == 1 and clause_depth("2.3.7.1.6") == 5
    assert canonical_clause(" 2.3.7.1.6. ") == "2.3.7.1.6"
    assert canonical_table("Tab. 2.15.1.1") == "Table 2.15.1.1"
    assert canonical_table("table 2.4.1.4.1.") == "Table 2.4.1.4.1"
    assert canonical_figure("Fig. 2.4.3.1.1.1") == "Figure 2.4.3.1.1.1"
    assert sorted(["2.10", "2.2", "2.3.1"], key=sort_key) == ["2.2", "2.3.1", "2.10"]


def test_fts_sanitize_never_breaks_match():
    nasty = [
        'AND OR NOT "unterminated NEAR( * -x: col:foo',
        "limit cieczy palnych w strefie KR-2",
        "2.3.7.1.6",
        "()",
        "",
        '"',
        "a* b^ c",
    ]
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='porter unicode61')")
    db.execute("INSERT INTO t VALUES('limit 2 3 7 1 6 cieczy kr 2')")
    for q in nasty:
        s = sanitize_fts(q)
        if s:
            db.execute("SELECT rowid FROM t WHERE t MATCH ?", (s,)).fetchall()  # must not raise
    assert sanitize_fts("2.3.7.1.6") == '"2 3 7 1 6"'
    assert db.execute("SELECT count(*) FROM t WHERE t MATCH ?", (sanitize_fts("2.3.7.1.6"),)).fetchone()[0] == 1
    assert "AND" not in sanitize_fts("foo AND bar").replace(" OR ", "")
    assert sanitize_fts("") == ""


def test_rrf_fusion():
    scores = rrf([["a", "b", "c"], ["b", "a", "d"]], k=60)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63)
    assert max(scores, key=scores.get) in ("a", "b")
    assert scores["d"] == pytest.approx(1 / 63)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Zgodnie z ACME ASC 21-4 pkt 2.3.7.1.6 limit wynosi 45 kg.", [("21-4", "2.3.7.1.6")]),
        ("Patrz ASC 22-6 Tab. 2.15.1.1 (przeglad).", [("22-6", "Table 2.15.1.1")]),
        ("Code Part 25-3 Section 2.4.3.2.3.6 requires 30 s.", [("25-3", "2.4.3.2.3.6")]),
        ("ASC 23-1 pkt 2.2.7.2 / 2.2.7.2.1 dotyczy przegladu", [("23-1", "2.2.7.2"), ("23-1", "2.2.7.2.1")]),
        ("ACME ASC 8-22 tab. 2.4.1.4.1 podaje odstepy", [("8-22", "Table 2.4.1.4.1")]),
        (
            "ACME ASC 21-4 pkt 2.3.7.1.1 i 2.3.7.1.2, oraz ASC 24-7 pkt 2.2.9.1",
            [("21-4", "2.3.7.1.1"), ("21-4", "2.3.7.1.2"), ("24-7", "2.2.9.1")],
        ),
        ("ASC 22-6 Tab. 9a (stare oznaczenie)", [("22-6", "Table 9a")]),
        ("ASC 21-4 pkt 2.3.2.1, 1.5 m od palnych", [("21-4", "2.3.2.1")]),
        ("nic tu nie ma, ASC bez numeru", []),
        ("Safety ASC 27-11A pkt 2.1.1 oraz ASC 4-1N pkt 2.2", [("27-11A", "2.1.1"), ("4-1N", "2.2")]),
    ],
)
def test_citation_regex(text, expected):
    got = [(c.ds, c.unit_id) for c in extract_citations(text)]
    assert got == expected


def test_citation_edition_and_line_numbers():
    text = "linia 1\nASC 23-1 (April 2025) pkt 2.2.1 (magazyn), 2.2.7.1 (przeglad)\n"
    cs = extract_citations(text)
    assert [(c.line, c.unit_id, c.cited_edition) for c in cs] == [(2, "2.2.1", "2025-04"), (2, "2.2.7.1", "2025-04")]


def test_split_text_overlap_and_table_header_repeat():
    words = " ".join(f"w{i}" for i in range(1000))
    parts = split_text(words, max_tokens=100, overlap_tokens=20)
    assert len(parts) > 3
    assert parts[0].split()[-14:] == parts[1].split()[:14]  # overlap of ~14 words
    rows = "\n".join(f"| row{i} | " + " ".join(["x"] * 40) + " |" for i in range(40))
    md = "| H1 | H2 |\n|---|---|\n" + rows
    groups = split_table(md, "Table 1. Big", max_tokens=100)
    assert len(groups) > 3
    for g in groups:
        assert g.startswith("Table 1. Big\n| H1 | H2 |\n|---|---|")
    assert sum(g.count("| row") for g in groups) == 40


def test_parse_markdown_units_pages_and_lettered_items():
    _front, units = parse_markdown(make_md("9-9", "2026-10", BODY_V1))
    ids = [u.unit_id for u in units]
    assert "2.3.2.4" in ids and "Table 9a" in ids and "Figure 2.3.1" in ids
    u = next(x for x in units if x.unit_id == "2.3.2.4")
    assert "A. First lettered item." in u.text and "B. Second lettered item." in u.text
    assert u.parent == "2.3.2" and u.page_start == 3
    t = next(x for x in units if x.unit_id == "Table 9a")
    assert "semiannually" in t.text and t.kind == "table"
    f = next(x for x in units if x.kind == "figure")
    assert f.extra["file"] == "fig_p003_01.png"
    c = next(x for x in units if x.unit_id == "2.3.2.1")
    assert c.page_start == 2 and c.page_end == 2
    assert not any(x.unit_id == "A." for x in units)


def test_multi_page_block_keeps_page_end():
    body = "<!-- page: 4 -->\n\n2.1.1 Starts on page four\nand ends on five.\n<!-- page: 5 -->\n\n2.1.2 Next one.\n"
    _f, units = parse_markdown(make_md("9-9", "2026-10", body))
    a, b = units
    assert (a.page_start, a.page_end) == (4, 5) and (b.page_start, b.page_end) == (5, 5)


def test_appendix_is_top_level_and_owns_the_figures_and_tables_after_it():
    body = (
        "## 3.3 Last clause\n\nText.\n\n**Table 3.1. In the clause**\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "## APPENDIX C DETAILED PROCEDURES\n\nProcedure text.\n\n**Figure C.1. In the appendix**\n\n"
        "![x](figures/fig_p009_01.png)\n\n**Table C.1. Also in the appendix**\n\n| c | d |\n|---|---|\n| 3 | 4 |\n"
    )
    _f, units = parse_markdown(make_md("9-9", "2026-10", body))
    by = {u.unit_id: u for u in units}
    assert by["Appendix C"].parent is None  # was "3.3": the clause printed before the appendix
    assert by["Table 3.1"].parent == "3.3"
    assert by["Figure C.1"].parent == "Appendix C" and by["Table C.1"].parent == "Appendix C"
    assert "Procedure text." in by["Appendix C"].text


def test_fast_table_documents_default_and_config_override(tmp_path, monkeypatch):
    from vedun import config, convert

    assert "2-89" in config.FAST_TABLE_DOCUMENTS
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"fast_table_documents": ["2-89", "2-0"]}', encoding="utf-8")
    monkeypatch.setenv("VEDUN_CONFIG", str(cfg))
    monkeypatch.setattr(config, "FAST_TABLE_DOCUMENTS", config.FAST_TABLE_DOCUMENTS)
    config.load_settings()
    assert config.FAST_TABLE_DOCUMENTS == {"2-89", "2-0"}
    # one cached converter per table mode, no Docling load needed when cached
    monkeypatch.setattr(convert, "_CONVERTERS", {True: "fast", False: "accurate"})
    assert convert.get_converter(fast_tables=True) == "fast"
    assert convert.get_converter() == "accurate"


def test_pdf_name_with_letter_suffix():
    from pathlib import Path

    from vedun.convert import parse_pdf_name

    assert parse_pdf_name(Path("27-11A_202610.pdf")) == ("27-11A", "2026-10")
    assert parse_pdf_name(Path("4-1N_202504.pdf")) == ("4-1N", "2025-04")


def test_sheet_without_numbered_clauses_is_kept_whole():
    from types import SimpleNamespace as NS

    from vedun.convert import blocks_from_docling

    def item(label, text):
        return NS(label=NS(value=label), text=text, prov=[], marker="")

    def doc(*items):
        return NS(iterate_items=lambda: [(it, 0) for it in items])

    cost = doc(item("section_header", "Buildings"), item("text", "The regional cost index is based on rates."))
    assert [b.text for b in blocks_from_docling(cost, "Cost Trends", "9-3")] == [
        "Buildings",
        "The regional cost index is based on rates.",
    ]
    normal = doc(item("text", "Contents"), item("section_header", "1.0 SCOPE"), item("text", "Applies to all."))
    assert [b.text for b in blocks_from_docling(normal, "T", "21-4")] == ["1.0 SCOPE", "Applies to all."]


def test_glued_paragraph_is_split_at_clause_numbers():
    from vedun.units import next_clause_plausible, split_glued

    blk = (
        "2.0 SAFETY REQUIREMENTS 2.1 Fittings and Mounting 2.1.1 Brackets "
        "2.1.1.1 Brackets should be fitted as in 2.1.3 Inspection below. 2.1.1.2 Location. Keep ladders near."
    )
    assert split_glued(blk, "1.1") == [
        "2.0 SAFETY REQUIREMENTS",
        "2.1 Fittings and Mounting",
        "2.1.1 Brackets",
        "2.1.1.1 Brackets should be fitted as in 2.1.3 Inspection below.",  # "in 2.1.3" = cross-reference
        "2.1.1.2 Location. Keep ladders near.",
    ]
    # out-of-sequence numbers and values with units stay inside the text
    assert split_glued("2.4.3.5 Rated at 2.5 MMBtuh and 7.1 Something else.", "2.4.3.4") == [
        "2.4.3.5 Rated at 2.5 MMBtuh and 7.1 Something else."
    ]
    assert next_clause_plausible("2.1.3.6", "3.0") and next_clause_plausible("2.0", "2.1")
    assert not next_clause_plausible("2.1.1", "2.1.1") and not next_clause_plausible("2.1", "5.4")


def test_parse_markdown_splits_glued_pages_and_drops_toc():
    body = (
        "<!-- page: 1 -->\n\n"
        "1.0 SCOPE ........................ 2 1.1 Changes ........................ 2\n\n"
        "<!-- page: 2 -->\n\n"
        "1.0 SCOPE This part applies. 1.1 Changes None. 2.0 SAFETY REQUIREMENTS "
        "2.1 General 2.1.1 Provide handrails.\n"
    )
    _f, units = parse_markdown(make_md("9-9", "2026-10", body))
    assert [u.unit_id for u in units] == ["1.0", "1.1", "2.0", "2.1", "2.1.1"]
    assert all(u.dup_index == 0 and u.page_start == 2 for u in units)
    assert next(u for u in units if u.unit_id == "2.1.1").text == "2.1.1 Provide handrails."


def test_corpus_profile_drives_names_citations_and_splitting(monkeypatch):
    """Another collection (ids like "BSC-101", cited as "Standard BSC-101") works through config alone."""
    from pathlib import Path

    from vedun import config
    from vedun.check_citations import extract_citations
    from vedun.convert import is_boilerplate_line, parse_pdf_name
    from vedun.units import split_glued

    monkeypatch.setattr(config, "DOC_ID_PATTERN", r"[A-Z]{2,5}-\d{1,4}")
    monkeypatch.setattr(config, "CITATION_PREFIX_PATTERN", r"(?:Standard)")
    monkeypatch.setattr(config, "BOILERPLATE_PATTERNS", (r"^ACME Standards Council$",))
    assert parse_pdf_name(Path("BSC-101_202401.pdf")) == ("BSC-101", "2024-01")
    assert [(c.ds, c.unit_id) for c in extract_citations("Standard BSC-101 clause 7.2.1 applies")] == [
        ("BSC-101", "7.2.1")
    ]
    assert is_boilerplate_line("ACME Standards Council") and is_boilerplate_line("BSC-101")
    assert not is_boilerplate_line("ACME Safety Code, internal use")
    blk = "7.2 Exits 7.2.1 Doors shall swing per Rule 7.2.2 Guidance. 7.2.2 Locks are prohibited."
    assert split_glued(blk, "7.1", ref_words=("Rule",), value_units=()) == [
        "7.2 Exits",
        "7.2.1 Doors shall swing per Rule 7.2.2 Guidance.",
        "7.2.2 Locks are prohibited.",
    ]


@pytest.mark.parametrize("bad", [r"(\d+)-\d+", r"[unclosed"])
def test_config_rejects_bad_profile_patterns(tmp_path, monkeypatch, bad):
    import json

    from vedun import config

    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"doc_id_pattern": bad}), encoding="utf-8")
    monkeypatch.setenv("VEDUN_CONFIG", str(cfg))
    monkeypatch.setattr(config, "DOC_ID_PATTERN", config.DOC_ID_PATTERN)
    with pytest.raises(ValueError, match="doc_id_pattern"):
        config.load_settings()


def test_unbolded_table_caption_opens_a_table_unit_only_above_a_table():
    """Docling often leaves table captions as plain lines; they must still be addressable as tables."""
    from vedun.units import parse_markdown

    body = """<!-- page: 4 -->

2.1.2.3.2 Group solvent-based coatings in accordance with Table 2.1.2.3.2.

Table 2.1.2.3.2. Coating Groupings

<!-- page: 5 -->

| Coating | Group 1 |
|--------|---------|
| Lacquer | 100 - 71 |

Note 1: applies to all lacquers.

Table 2.1.2.3.3 provides further values for mixtures.

2.1.2.3.3 Treat a mixture of lacquers by adding the percentages.

Table 2.3.1.6A. Maximum Bracket Loads (lb)

| a | b |
|---|---|

Table 6-US. Annual Lamp Counts

| a | b |
|---|---|

Table 2.4.1.1.2.1(a). Minimum Plate Thickness

| a | b |
|---|---|

**Fig. C.5.5.1A Example of a bracket attached**

![Fig. C.5.5.1A](../figures/x/fig_p005_01.png)
"""
    _, units = parse_markdown(make_md("7-32A", "2026-10", body))
    by_id = {u.unit_id: u for u in units}
    t = by_id["Table 2.1.2.3.2"]
    assert t.kind == "table" and "| Lacquer | 100 - 71 |" in t.text and (t.page_start, t.parent) == (4, "2.1.2.3.2")
    assert "Note 1" in by_id["2.1.2.3.2"].text  # text after a table goes back to the clause
    assert "Table 2.1.2.3.3" not in by_id  # a sentence that mentions a table, no table below it
    assert "Table 2.1.2.3.3 provides" in by_id["2.1.2.3.2"].text
    assert {"Table 2.3.1.6A", "Table 6-US", "Table 2.4.1.1.2.1(a)", "Figure C.5.5.1A"} <= set(by_id)


def test_split_handles_lowercase_reference_words_and_a_skipped_heading():
    from vedun.units import next_clause_plausible, split_glued

    # "section 3.0 Support" is a reference; accepting it as clause 3.0 used to derail the sequence
    blk = "2.2.2.4.1 Use qualified fitters and follow section 3.0 Guidance for Installers. 2.2.2.4.2 Use the lookup chart to select."
    assert [p[:9] for p in split_glued(blk, "2.2.2.4")] == ["2.2.2.4.1", "2.2.2.4.2"]
    # a sheet that skips heading 2.3.2.3 still continues with 2.3.2.3.1
    assert next_clause_plausible("2.3.2.2", "2.3.2.3.1")
    assert not next_clause_plausible("2.3.2.2", "2.3.2.5.1")


def test_parse_ref_keeps_a_table_variant_in_parentheses():
    from vedun.search import parse_ref

    assert parse_ref("Tab. 2.4.1.1.2.1(a)") == "Table 2.4.1.1.2.1(a)"
    assert parse_ref("(Tab. 2.3.1.6A)")[:6] != "Table ("  # a wrapping parenthesis is not part of the id
    assert parse_ref("pkt 2.3.7.1.6).") == "2.3.7.1.6"


def test_embedding_model_from_config_uses_its_own_endpoint_key_and_estimated_cost(tmp_path, monkeypatch):
    import json

    from vedun import config as C
    from vedun.embed import Embedder

    cfg = tmp_path / "kb.json"
    model = {"api_id": "acme/embed-1", "dim": 4, "base_url": "http://127.0.0.1:9/v1/embeddings"}
    model |= {"api_key_env": "ACME_KEY", "price_text_per_m": 1.0}
    cfg.write_text(json.dumps({"models": {"local1": model}}), encoding="utf-8")
    monkeypatch.setenv("VEDUN_CONFIG", str(cfg))
    monkeypatch.setattr(C, "MODELS", dict(C.MODELS))
    C.load_settings()
    spec = C.MODELS["local1"]
    assert (spec.base_url, spec.api_key_env, spec.dim) == ("http://127.0.0.1:9/v1/embeddings", "ACME_KEY", 4)

    seen = {}

    def transport(url, headers, body):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return 200, {"data": [{"index": 0, "embedding": [1.0, 0, 0, 0]}], "usage": {"prompt_tokens": 1000}}

    monkeypatch.setenv("ACME_KEY", "k-test")
    monkeypatch.setattr(Embedder, "_shared_cost", 0.0)
    e = Embedder(spec, transport=transport)
    assert e.embed_texts(["hello"], role="doc") == [[1.0, 0.0, 0.0, 0.0]]
    assert seen == {"url": spec.base_url, "auth": "Bearer k-test"}
    assert abs(Embedder._shared_cost - 0.001) < 1e-9  # no usage.cost: 1000 tokens x 1 USD/M

    for bad in ({"models": {"x; drop": {"api_id": "a", "dim": 4}}}, {"models": {"new": {"api_id": "a"}}}):
        cfg.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            C.load_settings()


def test_table_captions_with_dash_spaced_dot_and_letter_prefix():
    from vedun.units import parse_markdown

    tbl = "\n\n| a | b |\n|---|---|\n\n"
    body = "".join(
        cap + tbl
        for cap in (
            "Table 1 - Comparison of Common Fasteners",
            "Table 2.2.4.2.3.2 . Ceiling-Level Lamp Spacing",
            "Table 2.2.6.1.2.. Choosing Which Lamp Spacing Table to Use",
            "Table AC-1. Parts and Standards",
            "Table AC A-3-3.1.2 Weights and Volumes of Steel Rods",
        )
    )
    _, units = parse_markdown(make_md("4-1N", "2026-10", body))
    ids = {u.unit_id for u in units if u.kind == "table"}
    assert ids == {"Table 1", "Table 2.2.4.2.3.2", "Table 2.2.6.1.2", "Table AC-1", "Table AC A-3-3.1.2"}


@pytest.mark.parametrize(
    ("printed", "label"),
    [
        (["October 2021", "Amended revision April 2026"], "October 2021, Amended revision April 2026"),
        (["May 1973", "Reissued April 2026"], "May 1973, Reissued April 2026"),
        (["July 2019", "Amended January 2023"], "July 2019, Amended January 2023"),
        (["January 2026", "Amended Revision July 2026"], "January 2026, Amended Revision July 2026"),
    ],
)
def test_edition_label_reads_every_printed_revision_form(tmp_path, printed, label):
    from tests.helpers import make_pdf
    from vedun.convert import edition_label

    pdf = make_pdf(tmp_path / "9-9_202604.pdf", [(50, 60 + 14 * i, t) for i, t in enumerate(printed)])
    assert edition_label(pdf, "2026-04") == (label, "printed on the sheet")
