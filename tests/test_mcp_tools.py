"""MCP tools over the real app (revision 2026-07-28 on the wire): happy path, not found, invalid input, paging, caps,
budget. The knowledge base is the synthetic sheet 9-9 of ``tests/helpers_server.py``; no network, no real keys."""

from __future__ import annotations

import base64
import io
import json
import socket

import pytest

from tests.helpers_mcp import call, text_of
from vedun.server import api, audit, mcp_tools

QUOTE = "Limit paint stock to 45 kg (99 lb) per cabinet."
ALL_IDS = ["2.3", "2.3.2.1", "2.3.2.2", "2.3.2.3", "Table 9a", "Figure 2.3.1", "2.3.2.4"]


def err(result: dict) -> str:
    assert result["isError"] is True, result
    assert "structuredContent" not in result
    return text_of(result)


def ids(result: dict, key: str = "hits") -> list[str]:
    return [h["unit_id"] for h in result["structuredContent"][key]]


@pytest.fixture()
def no_paid(monkeypatch):
    """Any embedding / reranker construction fails the test: the code under test must stay on the free path."""
    calls: list[str] = []

    def boom(kind):
        def f(*a, **k):
            calls.append(kind)
            raise AssertionError(f"paid {kind} call attempted")

        return f

    monkeypatch.setattr(api.hooks, "embedder_factory", boom("embedder"))
    monkeypatch.setattr(api.hooks, "reranker_factory", boom("reranker"))
    return calls


# ============================================================================ kb_lookup
def test_lookup_happy_path_has_full_provenance(client):
    r = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "pkt 2.3.2.4"})
    assert r["isError"] is False
    sc = r["structuredContent"]
    assert sc["all_found"] and sc["document_indexed"] and sc["next_cursor"] is None
    ev = sc["items"][0]["evidence"]
    assert "45 kg (99 lb)" in ev["text"]
    assert ev["doc_id"] == "9-9" and ev["unit_id"] == "2.3.2.4" and ev["unit_kind"] == "clause"
    assert ev["document_title"] == "Test Sheet" and ev["edition"] == "2026-10" and ev["edition_label"]
    assert ev["parent_path"][0]["unit_id"] == "2.0"
    assert ev["citation_pl"].endswith("pkt 2.3.2.4") and ev["citation_en"].endswith("Section 2.3.2.4")
    assert "page" not in ev["citation_pl"].lower()
    assert ev["retrieval"]["match_type"] == "lookup"
    assert ev["source_pdf"] and ev["source_pdf_host"].endswith("9-9_202610.pdf")
    assert ev["md_path_host"].endswith("9-9_202610.md") and ev["md_line_start"] == 43
    assert "/" in ev["source_pdf_host"] and "\\" not in ev["source_pdf_host"]  # host paths use forward slashes
    # the text block is self-sufficient: citation + verbatim text
    t = text_of(r)
    assert ev["citation_pl"] in t and "45 kg (99 lb)" in t and ev["source_pdf_host"] in t


@pytest.mark.parametrize(
    ("ref", "unit_id"),
    [("Tab. 9a", "Table 9a"), ("Table 9a", "Table 9a"), ("rys. 2.3.1", "Figure 2.3.1"), ("Fig. 2.3.1", "Figure 2.3.1")],
)
def test_lookup_forgiving_ref_forms(client, ref, unit_id):
    r = call(client, "kb_lookup", {"doc_id": "9-9", "ref": ref})
    assert r["structuredContent"]["items"][0]["evidence"]["unit_id"] == unit_id


def test_lookup_several_refs_and_not_found_are_normal_results(client):
    r = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "2.3.2.1", "refs": ["2.3.2.2", "9.9.9", "2.3.2.1"]})
    assert r["isError"] is False  # not found is information, not a tool error
    sc = r["structuredContent"]
    assert [i["requested"] for i in sc["items"]] == ["2.3.2.1", "2.3.2.2", "9.9.9"]  # de-duplicated, order kept
    assert [i["found"] for i in sc["items"]] == [True, True, False]
    assert sc["all_found"] is False
    miss = sc["items"][2]
    assert miss["evidence"] is None and miss["nearest"] and "2.3.2" in miss["nearest"][0]
    assert 'NOT FOUND "9.9.9"' in text_of(r)


def test_lookup_unindexed_sheet_lists_what_is_indexed(client):
    r = call(client, "kb_lookup", {"doc_id": "9-8", "ref": "1.1"})
    assert r["isError"] is False
    assert r["structuredContent"]["document_indexed"] is False
    assert "Indexed documents: 9-9" in text_of(r)


def test_lookup_children_and_neighbors(client):
    r = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "2.3", "include_children": True, "neighbors": True})
    item = r["structuredContent"]["items"][0]
    assert item["evidence"]["unit_id"] == "2.3"
    assert [c["unit_id"] for c in item["children"]] == ALL_IDS[1:]
    assert item["prev_id"] == "2.0" and item["next_id"] == "2.3.2.1"


@pytest.mark.parametrize(
    "args",
    [
        {"doc_id": "9-9x", "ref": "1"},  # sheet number pattern
        {"doc_id": "../etc", "ref": "1"},
        {"doc_id": "9-9", "ref": ""},  # empty ref
        {"doc_id": "9-9", "ref": "x" * 81},  # too long
        {"doc_id": "9-9", "refs": ["1"] * 21},  # too many
        {"doc_id": "9-9", "ref": 5},  # wrong type
        {"doc_id": "9-9"},  # neither ref nor refs
    ],
)
def test_lookup_invalid_input_is_a_tool_error(client, args):
    assert "kb_lookup" in err(call(client, "kb_lookup", args))


# ============================================================================ kb_toc
def test_toc_whole_sheet_branch_and_errors(client):
    r = call(client, "kb_toc", {"doc_id": "9-9"})
    sc = r["structuredContent"]
    assert sc["total"] == 10 and sc["document_title"] == "Test Sheet" and sc["next_cursor"] is None
    assert [i["unit_id"] for i in sc["items"]][:2] == ["1.0", "1.2"]
    assert "(4 children)" in text_of(r)
    r = call(client, "kb_toc", {"doc_id": "9-9", "prefix": "2.3", "depth": 1})
    assert [i["unit_id"] for i in r["structuredContent"]["items"]] == [
        "2.3",
        "2.3.2.1",
        "2.3.2.2",
        "2.3.2.3",
        "2.3.2.4",
    ]
    assert "nearest" in err(call(client, "kb_toc", {"doc_id": "9-9", "prefix": "9.9.9"}))
    assert "Indexed documents: 9-9" in err(call(client, "kb_toc", {"doc_id": "9-8"}))
    err(call(client, "kb_toc", {"doc_id": "9-9", "depth": 0}))
    err(call(client, "kb_toc", {"doc_id": "9-9", "limit": 301}))


def test_toc_pagination_is_continuous(client):
    full = ids(call(client, "kb_toc", {"doc_id": "9-9"}), "items")
    got, cursor, pages = [], None, 0
    while True:
        r = call(client, "kb_toc", {"doc_id": "9-9", "limit": 3, **({"cursor": cursor} if cursor else {})})
        got += ids(r, "items")
        pages += 1
        cursor = r["structuredContent"]["next_cursor"]
        if not cursor:
            break
        assert "cursor=" + cursor in text_of(r)
    assert got == full and pages == 4


# ============================================================================ kb_fulltext
def test_fulltext_finds_exact_phrase_without_any_paid_call(client, no_paid, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    r = call(client, "kb_fulltext", {"query": '"45 kg"'})
    assert r["isError"] is False and ids(r) == ["2.3.2.4"]
    h = r["structuredContent"]["hits"][0]
    assert h["retrieval"]["match_type"] == "search" and h["retrieval"]["final_ranker"] == "bm25"
    assert h["citation_pl"] in text_of(r) and h["snippet"]
    r = call(client, "kb_fulltext", {"query": "45 kg", "doc_id": "9-9", "kind": "clause"})  # safe syntax: AND-ed terms
    assert ids(r) == ["2.3.2.4"]
    assert no_paid == []


def test_fulltext_fts5_operators_and_errors(client):
    r = call(client, "kb_fulltext", {"query": "provide NOT lights", "syntax": "fts5"})
    assert ids(r) == ["2.3.2.3"]
    r = call(client, "kb_fulltext", {"query": "NEAR(battery reserve, 3)", "syntax": "fts5"})
    assert ids(r) == ["2.3.2.3"]
    r = call(client, "kb_fulltext", {"query": "batt*", "syntax": "fts5"})
    assert "2.3.2.3" in ids(r)
    msg = err(call(client, "kb_fulltext", {"query": '"45 kg', "syntax": "fts5"}))  # unterminated phrase
    assert "invalid FTS5 expression" in msg and "Hint" in msg
    assert "invalid FTS5" in err(call(client, "kb_fulltext", {"query": "AND AND (", "syntax": "fts5"}))
    # the safe syntax never raises on punctuation
    assert call(client, "kb_fulltext", {"query": '(("45" kg ) NEAR/ * AND', "syntax": "safe"})["isError"] is False
    err(call(client, "kb_fulltext", {"query": "x", "syntax": "sql"}))
    err(call(client, "kb_fulltext", {"query": ""}))
    err(call(client, "kb_fulltext", {"query": "x" * 501}))
    err(call(client, "kb_fulltext", {"query": "x", "limit": 31}))
    err(call(client, "kb_fulltext", {"query": "x", "doc_id": "bad"}))


def test_fulltext_pagination_is_continuous_and_bound_to_the_query(client):
    q = {"query": "Provide OR Limit OR Treat OR Inspect", "syntax": "fts5"}
    full = ids(call(client, "kb_fulltext", {**q, "limit": 10}))
    assert len(full) >= 4
    got, cursor = [], None
    while True:
        r = call(client, "kb_fulltext", {**q, "limit": 1, **({"cursor": cursor} if cursor else {})})
        got += ids(r)
        cursor = r["structuredContent"]["next_cursor"]
        if not cursor:
            break
        last = cursor
    assert got == full
    assert "different query" in err(call(client, "kb_fulltext", {**q, "query": "Provide", "cursor": last}))
    assert "invalid cursor" in err(call(client, "kb_fulltext", {**q, "cursor": "not-a-cursor"}))
    assert "invalid cursor" in err(call(client, "kb_toc", {"doc_id": "9-9", "cursor": last}))  # another tool's cursor


# ============================================================================ kb_search
def test_search_hybrid_returns_hits_and_reports_degradation(client):
    r = call(client, "kb_search", {"query": "limit on paint per cabinet"})
    sc = r["structuredContent"]
    assert r["isError"] is False and sc["hits"] and sc["mode"] == "hybrid" and sc["budget_exhausted"] is False
    assert "2.3.2.4" in ids(r)
    # no TYPESAFE_API_KEY in the tests: the reranker is unavailable, which must be stated, not hidden
    assert sc["reranked"] is False and sc["rerank_error"] and any("rerank" in d for d in sc["degraded"])
    assert len([d for d in sc["degraded"] if sc["rerank_error"] in d]) == 1  # not repeated
    assert sc["hits"][0]["retrieval"]["final_ranker"].startswith("wrrf")
    assert "NOTE:" in text_of(r)
    r = call(client, "kb_search", {"query": "limit on paint per cabinet", "rerank": "none", "w_fts": 1, "w_vec": 0})
    assert r["structuredContent"]["w_fts"] == 1 and r["structuredContent"]["rerank_error"] is None


def test_search_budget_exhausted_falls_back_to_bm25_and_never_calls_a_paid_api(client, no_paid, monkeypatch):
    monkeypatch.setenv("VEDUN_DAILY_BUDGET_USD", "0")
    r = call(client, "kb_search", {"query": "paint cabinet", "rerank": "r3", "include_figures": True})
    sc = r["structuredContent"]
    assert r["isError"] is False
    assert sc["budget_exhausted"] is True and sc["mode"] == "bm25" and sc["reranker"] in (None, "")
    assert "2.3.2.4" in ids(r)
    assert any("dzienny budżet API wyczerpany" in d for d in sc["degraded"])
    assert "dzienny budżet API wyczerpany" in text_of(r)
    assert no_paid == []  # neither an embedder nor a reranker was even constructed


def test_search_pagination_and_validation(client):
    q = {"query": "provide battery paint storage", "rerank": "none", "include_figures": False}
    full = ids(call(client, "kb_search", {**q, "limit": 20}))
    got, cursor = [], None
    for _ in range(30):
        r = call(client, "kb_search", {**q, "limit": 2, **({"cursor": cursor} if cursor else {})})
        got += ids(r)
        cursor = r["structuredContent"]["next_cursor"]
        if not cursor:
            break
    assert got == full and len(full) > 2
    err(call(client, "kb_search", {"query": "x", "limit": 21}))
    err(call(client, "kb_search", {"query": "x", "w_vec": 1.5}))
    err(call(client, "kb_search", {"query": "x", "rerank": "gpt"}))
    err(call(client, "kb_search", {"query": "x" * 501}))


def test_search_backend_failure_is_a_tool_error_not_an_exception(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internals sk-should-not-leak")

    monkeypatch.setattr(mcp_tools.kb, "search", boom)
    msg = err(call(client, "kb_search", {"query": "x"}))
    assert "retrieval failed" in msg and "kb_fulltext" in msg


# ============================================================================ kb_get_document
def test_document_meta_md_pdf(client, server_kb):
    r = call(client, "kb_get_document", {"doc_id": "9-9"})
    sc = r["structuredContent"]
    assert sc["found"] and sc["format"] == "meta" and sc["title"] == "Test Sheet" and sc["pages"] == 3
    assert sc["paths"]["pdf_path_host"].endswith("9-9_202610.pdf") and sc["paths"]["md_exists"]
    assert [t["unit_id"] for t in sc["toc"]] == ["1.0", "2.0"]
    assert sc["citation_prefix"].startswith("ASC 9-9") and sc["paths"]["pdf_path_host"] in text_of(r)
    r = call(client, "kb_get_document", {"doc_id": "9-9", "fmt": "md", "section": "2.3"})
    sc = r["structuredContent"]
    assert sc["section"] == "2.3" and "45 kg (99 lb)" in sc["text"] and sc["text"] in text_of(r)
    assert sc["next_cursor"] is None and sc["returned_chars"] == sc["total_chars"] == len(sc["text"])
    r = call(client, "kb_get_document", {"doc_id": "9-9", "fmt": "pdf"})
    sc = r["structuredContent"]
    assert sc["url"] == "/api/documents/9-9.pdf" and sc["text"] is None
    assert "%PDF" not in json.dumps(r)  # never the bytes


def test_document_md_pagination_reassembles_the_file(client, server_kb):
    md = server_kb.root / "md" / "9-9_202610.md"
    filler = "".join(f"Extra paragraph number {i} of filler text.\n\n" for i in range(60))
    md.write_text(md.read_text(encoding="utf-8") + filler, encoding="utf-8")
    full = md.read_text(encoding="utf-8")
    assert len(full) > 3000
    got, cursor, pages = "", None, 0
    while True:
        r = call(
            client,
            "kb_get_document",
            {"doc_id": "9-9", "fmt": "md", "max_chars": 1000, **({"cursor": cursor} if cursor else {})},
        )
        sc = r["structuredContent"]
        assert sc["offset"] == len(got) and len(sc["text"]) <= 1000
        got += sc["text"]
        pages += 1
        cursor = sc["next_cursor"]
        if not cursor:
            break
    assert got == full and pages >= 2


def test_document_errors(client):
    assert "Indexed documents" in err(call(client, "kb_get_document", {"doc_id": "9-8"}))
    assert "Nearest ids" in err(call(client, "kb_get_document", {"doc_id": "9-9", "fmt": "md", "section": "9.9.9"}))
    err(call(client, "kb_get_document", {"doc_id": "9-9", "fmt": "html"}))
    err(call(client, "kb_get_document", {"doc_id": "9-9", "fmt": "md", "section": "../../etc/passwd"}))
    err(call(client, "kb_get_document", {"doc_id": "9-9", "fmt": "md", "max_chars": 20_001}))
    err(call(client, "kb_get_document", {"doc_id": "9-9", "fmt": "md", "max_chars": 10}))


# ============================================================================ images
def test_figure_returns_png_without_structured_content(client, server_kb):
    r = call(client, "kb_figure", {"doc_id": "9-9", "figure_id": "Fig. 2.3.1"})
    assert r["isError"] is False and "structuredContent" not in r
    img = [c for c in r["content"] if c["type"] == "image"]
    assert len(img) == 1 and img[0]["mimeType"] == "image/png"
    assert base64.b64decode(img[0]["data"]).startswith(b"\x89PNG")
    t = text_of(r)
    assert "rys. 2.3.1" in t and "fig_p003_01.png" in t and "Test Sheet" in t


def test_figure_too_large_is_not_attached(client, monkeypatch):
    monkeypatch.setattr(mcp_tools, "MAX_IMAGE_BYTES", 10)
    r = call(client, "kb_figure", {"doc_id": "9-9", "figure_id": "2.3.1"})
    assert not [c for c in r["content"] if c["type"] == "image"]
    assert "NOT attached" in text_of(r) and "fig_p003_01.png" in text_of(r)


def test_png_is_downscaled_to_fit(tmp_path, monkeypatch):
    from PIL import Image

    p = tmp_path / "big.png"
    Image.frombytes("RGB", (800, 800), bytes(range(256)) * 7500).save(p)  # noisy-ish, large raw file
    big = p.stat().st_size
    monkeypatch.setattr(mcp_tools, "MAX_IMAGE_BYTES", big - 1)
    monkeypatch.setattr(mcp_tools, "MAX_IMAGE_SIDE", 100)
    data, note = mcp_tools._png_for_model(p)
    assert data is not None and len(data) < big and "downscaled" in note
    assert Image.open(io.BytesIO(data)).size[0] <= 100


def test_figure_and_page_errors(client):
    assert "known figures" in err(call(client, "kb_figure", {"doc_id": "9-9", "figure_id": "Fig. 9.9.9"}))
    assert "Indexed documents: 9-9" in err(call(client, "kb_figure", {"doc_id": "9-8", "figure_id": "Fig. 1"}))
    err(call(client, "kb_figure", {"doc_id": "9-9", "figure_id": ""}))
    assert "3 pages" in err(call(client, "kb_page_image", {"doc_id": "9-9", "page": 99}))
    err(call(client, "kb_page_image", {"doc_id": "9-9", "page": 0}))


def test_page_that_cannot_be_rendered_is_a_tool_error(client):
    # the synthetic PDF has one page while the index claims three
    assert "cannot render page 3" in err(call(client, "kb_page_image", {"doc_id": "9-9", "page": 3}))


def test_page_image(client):
    r = call(client, "kb_page_image", {"doc_id": "9-9", "page": 1})
    img = [c for c in r["content"] if c["type"] == "image"]
    assert len(img) == 1 and base64.b64decode(img[0]["data"]).startswith(b"\x89PNG")
    assert "page 1" in text_of(r) and "structuredContent" not in r


# ============================================================================ kb_verify_quote
def test_verify_quote_ok_and_altered_number_fails(client):
    r = call(client, "kb_verify_quote", {"doc_id": "9-9", "ref": "pkt 2.3.2.4", "quote": QUOTE})
    sc = r["structuredContent"]
    assert r["isError"] is False and sc["verdict"] == "OK" and sc["match"] and sc["in_unit"] and sc["in_pdf"]
    assert sc["evidence"]["citation_pl"].endswith("pkt 2.3.2.4") and text_of(r).startswith("OK:")
    bad = call(client, "kb_verify_quote", {"doc_id": "9-9", "ref": "2.3.2.4", "quote": QUOTE.replace("99", "90")})
    sc = bad["structuredContent"]
    assert bad["isError"] is False  # NO MATCH is a verdict, not a failure of the tool
    assert sc["verdict"] == "NO MATCH" and not sc["match"]
    assert sc["closest_unit_id"] == "2.3.2.4" and sc["closest_ratio"] > 0.9 and "99" in sc["closest"]
    assert sc["closest_citation_pl"].endswith("pkt 2.3.2.4") and "NO MATCH" in text_of(bad)


def test_verify_quote_names_the_unit_that_really_contains_it(client):
    r = call(client, "kb_verify_quote", {"doc_id": "9-9", "ref": "2.3.2.1", "quote": QUOTE})
    sc = r["structuredContent"]
    assert sc["verdict"] == "NO MATCH" and sc["found_in_units"] == ["2.3.2.4"]
    assert sc["found_in_citations_pl"][0].endswith("pkt 2.3.2.4")
    assert "IS verbatim in" in text_of(r)


def test_verify_quote_edge_cases(client):
    short = call(client, "kb_verify_quote", {"doc_id": "9-9", "ref": "2.3.2.4", "quote": "45 kg"})
    assert short["structuredContent"]["verdict"] == "NO MATCH" and "too short" in short["structuredContent"]["message"]
    gone = call(client, "kb_verify_quote", {"doc_id": "9-9", "ref": "9.9.9", "quote": QUOTE})
    assert gone["structuredContent"]["verdict"] == "NO MATCH" and "no unit" in gone["structuredContent"]["message"]
    other = call(client, "kb_verify_quote", {"doc_id": "9-8", "ref": "1", "quote": QUOTE})
    assert "not indexed" in other["structuredContent"]["message"]
    err(call(client, "kb_verify_quote", {"doc_id": "9-9", "ref": "2.3.2.4", "quote": "x" * 3001}))
    err(call(client, "kb_verify_quote", {"doc_id": "9-9", "ref": "2.3.2.4", "quote": ""}))
    err(call(client, "kb_verify_quote", {"doc_id": "bad", "ref": "2.3.2.4", "quote": QUOTE}))


# ============================================================================ kb_documents / kb_check_citations
def test_documents(client):
    r = call(client, "kb_documents", {})
    sc = r["structuredContent"]
    assert sc["total"] == 1 and sc["documents"][0]["doc_id"] == "9-9" and sc["documents"][0]["indexed"] is True
    assert sc["documents"][0]["edition"] == "2026-10" and sc["documents"][0]["catalog_status"] == "unknown"
    assert sc["documents"][0]["n_units"] == 10 and sc["counts"] == {"unknown": 1}
    assert call(client, "kb_documents", {"filter": "zzz"})["structuredContent"]["documents"] == []
    assert call(client, "kb_documents", {"filter": "test sheet"})["structuredContent"]["total"] == 1
    scope_all = call(client, "kb_documents", {"scope": "all"})["structuredContent"]
    assert any(s["doc_id"] == "9-9" for s in scope_all["documents"])
    err(call(client, "kb_documents", {"scope": "everything"}))
    err(call(client, "kb_documents", {"filter": "x" * 101}))


def test_documents_pagination(client, monkeypatch):
    monkeypatch.setattr(mcp_tools.kb, "list_documents", lambda conn, f=None: [])
    r = call(client, "kb_documents", {"scope": "all", "limit": 1})
    assert r["isError"] is False


def test_check_citations(client):
    text = "see ASC 9-9 pkt 2.3.2.4, ASC 9-9 pkt 9.9.9 and ASC 3-26 pkt 1.1.1"
    r = call(client, "kb_check_citations", {"text": text})
    sc = r["structuredContent"]
    assert sc["total"] == 3 and sc["all_ok"] is False
    assert sc["counts"] == {"OK": 1, "UNKNOWN UNIT": 1, "DOCUMENT NOT INDEXED": 1}
    assert [x["status"] for x in sc["results"]] == ["OK", "UNKNOWN UNIT", "DOCUMENT NOT INDEXED"]
    assert "nearest" in sc["results"][1]["suggestion"]
    ok = call(client, "kb_check_citations", {"text": "ASC 9-9 pkt 2.3.2.4"})["structuredContent"]
    assert ok["all_ok"] is True
    none = call(client, "kb_check_citations", {"text": "no citations here"})["structuredContent"]
    assert none["total"] == 0 and none["all_ok"] is False
    err(call(client, "kb_check_citations", {"text": ""}))
    err(call(client, "kb_check_citations", {"text": "x" * 50_001}))


def test_check_citations_adversarial_text_is_fast(client):
    import time

    t0 = time.time()
    call(client, "kb_check_citations", {"text": ("DS " + "9-9 " * 20 + "pkt " + "1." * 40) * 400})
    call(client, "kb_check_citations", {"text": "(" * 49_000})
    assert time.time() - t0 < 20


# ============================================================================ caps and paging of big results
def test_lookup_pages_by_size_and_reassembles(client, monkeypatch):
    full = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "2.3", "include_children": True})
    want = [full["structuredContent"]["items"][0]["evidence"]["unit_id"]] + [
        c["unit_id"] for c in full["structuredContent"]["items"][0]["children"]
    ]
    monkeypatch.setattr(mcp_tools, "MAX_TEXT_CHARS", 1500)
    got, cursor, pages = [], None, 0
    while True:
        r = call(
            client,
            "kb_lookup",
            {"doc_id": "9-9", "ref": "2.3", "include_children": True, **({"cursor": cursor} if cursor else {})},
        )
        item = r["structuredContent"]["items"][0]
        if item["evidence"]:
            got.append(item["evidence"]["unit_id"])
        got += [c["unit_id"] for c in item["children"]]
        if pages:
            assert item["continued"] is True and item["evidence"] is None
        pages += 1
        cursor = r["structuredContent"]["next_cursor"]
        if not cursor:
            break
        assert pages < 20
    assert got == want and pages > 1
    assert len(want) == 7


def test_lookup_windows_an_oversized_unit_text(client, monkeypatch):
    whole = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "2.3.2.4"})["structuredContent"]["items"][0]["evidence"][
        "text"
    ]
    monkeypatch.setattr(mcp_tools, "MAX_TEXT_CHARS", 790)  # budget 90 chars per page after the 700 chars of overhead
    monkeypatch.setattr(mcp_tools, "MIN_WINDOW_CHARS", 30)
    got, cursor, pages = "", None, 0
    while True:
        r = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "2.3.2.4", **({"cursor": cursor} if cursor else {})})
        ev = r["structuredContent"]["items"][0]["evidence"]
        assert ev["text_offset"] == len(got) and ev["text_chars"] == len(whole)
        assert ev["text_truncated"] == bool(r["structuredContent"]["next_cursor"] or ev["text_offset"])
        got += ev["text"]
        pages += 1
        cursor = r["structuredContent"]["next_cursor"]
        if not cursor:
            break
        assert pages < 20
    assert got == whole and pages > 1


def test_hit_pages_respect_the_json_and_text_caps(client, monkeypatch):
    monkeypatch.setattr(mcp_tools, "MAX_JSON_CHARS", 2500)  # one hit is about 2.2k characters of JSON
    r = call(client, "kb_fulltext", {"query": "Provide OR Limit OR Treat OR Inspect", "syntax": "fts5", "limit": 10})
    assert len(r["structuredContent"]["hits"]) < 4 and r["structuredContent"]["next_cursor"]
    assert len(json.dumps(r["structuredContent"])) < 3500
    monkeypatch.setattr(mcp_tools, "MAX_JSON_CHARS", 40_000)
    monkeypatch.setattr(mcp_tools, "MAX_TEXT_CHARS", 1)  # even then one hit always goes through
    r = call(client, "kb_fulltext", {"query": "Provide", "limit": 10})
    assert len(r["structuredContent"]["hits"]) == 1 and r["structuredContent"]["next_cursor"]


def test_default_caps_keep_a_page_under_the_client_limits():
    # Claude Code spills text results > 50 000 characters to a file and warns above 10 000 tokens
    assert mcp_tools.MAX_TEXT_CHARS <= 20_000 and mcp_tools.MAX_JSON_CHARS <= 40_000
    assert mcp_tools.MAX_TEXT_CHARS + mcp_tools.MAX_JSON_CHARS < 50_000 * 1.25


# ============================================================================ listing and misc
def test_tool_audit_logs_name_and_outcome_but_never_arguments(client):
    call(client, "kb_fulltext", {"query": "very-secret-needle-text"})
    err(call(client, "kb_fulltext", {"query": '"unterminated', "syntax": "fts5"}))
    rows = [r for r in audit.read_tail(50) if r["action"] == "mcp_tool"]
    assert [(r["detail"], r["outcome"], r["caller"]) for r in rows][-2:] == [
        ("kb_fulltext", "ok", "mcp"),
        ("kb_fulltext", "error", "mcp"),
    ]
    assert "needle" not in json.dumps(audit.read_tail(50)) and "unterminated" not in json.dumps(audit.read_tail(50))


def test_every_tool_has_output_schema_unless_it_returns_images(client):
    from tests.helpers_mcp import rpc

    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    image_tools = {"kb_figure", "kb_page_image"}
    for name, t in tools.items():
        assert ("outputSchema" in t) == (name not in image_tools), name
        for p in t["inputSchema"].get("properties", {}).values():
            assert p.get("description"), (name, p)  # every parameter is described
    # structured results of every other tool validate against the declared schema (the SDK enforces it; spot check)
    assert tools["kb_lookup"]["outputSchema"]["properties"]["items"]


def test_structured_results_validate_against_the_published_output_schemas(client):
    """What a strict client does: validate structuredContent against the outputSchema of tools/list."""
    from jsonschema import Draft202012Validator

    from tests.helpers_mcp import rpc

    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    calls = [
        ("kb_lookup", {"doc_id": "9-9", "refs": ["2.3.2.4", "9.9.9"], "neighbors": True}),
        ("kb_lookup", {"doc_id": "9-9", "ref": "2.3", "include_children": True}),
        ("kb_lookup", {"doc_id": "9-8", "ref": "1"}),
        ("kb_toc", {"doc_id": "9-9"}),
        ("kb_fulltext", {"query": "paint"}),
        ("kb_search", {"query": "paint per cabinet", "rerank": "none"}),
        ("kb_get_document", {"doc_id": "9-9"}),
        ("kb_get_document", {"doc_id": "9-9", "fmt": "md", "section": "2.3"}),
        ("kb_get_document", {"doc_id": "9-9", "fmt": "pdf"}),
        ("kb_verify_quote", {"doc_id": "9-9", "ref": "2.3.2.4", "quote": QUOTE}),
        ("kb_verify_quote", {"doc_id": "9-9", "ref": "2.3.2.1", "quote": QUOTE}),
        ("kb_documents", {"scope": "all"}),
        ("kb_check_citations", {"text": "ASC 9-9 pkt 2.3.2.4 and ASC 9-9 pkt 1.1.1"}),
    ]
    for name, args in calls:
        r = call(client, name, args)
        errors = list(Draft202012Validator(tools[name]["outputSchema"]).iter_errors(r["structuredContent"]))
        assert not errors, (name, args, [e.message[:200] for e in errors[:3]])


def test_host_paths_follow_the_container_mapping(client, monkeypatch):
    monkeypatch.setenv("VEDUN_HOST_ROOT", "C:/host/kb")
    monkeypatch.setenv("VEDUN_HOST_PDF_DIR", "C:/host/pdf")
    ev = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "pkt 2.3.2.4"})["structuredContent"]["items"][0]["evidence"]
    assert ev["source_pdf_host"] == "C:/host/pdf/9-9_202610.pdf"
    assert ev["md_path_host"] == "C:/host/kb/md/9-9_202610.md"
    fig = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "rys. 2.3.1"})["structuredContent"]["items"][0]["evidence"]
    assert fig["figure_png_host"] == "C:/host/kb/figures/9-9_202610/fig_p003_01.png"
    doc = call(client, "kb_get_document", {"doc_id": "9-9"})["structuredContent"]["paths"]
    assert doc["pdf_path_host"] == "C:/host/pdf/9-9_202610.pdf" and doc["md_path_host"] == "C:/host/kb/md/9-9_202610.md"


def test_crafted_cursors_are_tool_errors_never_crashes(client):
    import base64

    def cur(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    good = {"v": 1, "t": "kb_toc", "k": "x", "o": 0, "c": 0}
    for bad in (
        {k: v for k, v in good.items() if k != "k"},  # no key
        {**good, "o": -1},
        {**good, "o": True},
        {**good, "o": 10**12},
        {**good, "v": 2},
        {**good, "t": "kb_lookup"},
        [1, 2, 3],
        "text",
        7,
    ):
        msg = err(call(client, "kb_toc", {"doc_id": "9-9", "cursor": cur(bad)}))
        assert "cursor" in msg, bad
    for raw in ("", "!!!", "e30", "\u00e9\u00e9", "A" * 600):
        assert "cursor" in err(call(client, "kb_toc", {"doc_id": "9-9", "cursor": raw}))
    # a well-formed cursor for other arguments, and one past the end of the real result
    key_of = mcp_tools._args_key("9-9", None, None)
    assert "beyond the end" in err(
        call(client, "kb_toc", {"doc_id": "9-9", "cursor": cur({**good, "k": key_of, "o": 500})})
    )
    assert "beyond the end" in err(
        call(
            client,
            "kb_lookup",
            {
                "doc_id": "9-9",
                "ref": "2.3.2.4",
                "cursor": cur(
                    {**good, "t": "kb_lookup", "k": mcp_tools._args_key("9-9", ["2.3.2.4"], False, False), "o": 50}
                ),
            },
        )
    )


def test_tool_names_and_descriptions_take_the_profile_prefix(monkeypatch):
    from vedun import config as C
    from vedun.server import mcp_tools as M

    class Server:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name, title, description, annotations):
            self.tools[name] = description
            return lambda fn: fn

    monkeypatch.setattr(C, "MCP_TOOL_PREFIX", "acme")
    srv = Server()
    M.register_tools(srv)
    assert sorted(srv.tools) == sorted(n.replace("kb_", "acme_", 1) for n, *_ in M.TOOL_SPECS)
    assert "acme_search" in srv.tools["acme_lookup"] and "kb_" not in " ".join(srv.tools.values())
    assert (
        M.localize("kb_lookup, then kb_get_document; kb_other stays")
        == "acme_lookup, then acme_get_document; kb_other stays"
    )


OLD_KEYS = {"ds", "sheet_title", "fm_item_id", "sheet_indexed", "sheets"}


def _keys(o):
    """Every dict key in a JSON-like structure."""
    if isinstance(o, dict):
        return set(o) | {k for v in o.values() for k in _keys(v)}
    if isinstance(o, list):
        return {k for v in o for k in _keys(v)}
    return set()


def test_public_names_are_document_names_only(client):
    from tests.helpers_mcp import rpc

    sc = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "2.3.2.4"})["structuredContent"]
    ev = sc["items"][0]["evidence"]
    assert sc["doc_id"] == ev["doc_id"] == "9-9" and ev["document_title"] == "Test Sheet" and "source_item_id" in ev
    assert sc["document_indexed"] is True and not _keys(sc) & OLD_KEYS
    docs = call(client, "kb_documents", {})["structuredContent"]
    assert docs["documents"][0]["doc_id"] == "9-9" and not _keys(docs) & OLD_KEYS
    toc = call(client, "kb_toc", {"doc_id": "9-9"})["structuredContent"]
    assert toc["doc_id"] == "9-9" and toc["document_title"] and not _keys(toc) & OLD_KEYS
    assert not _keys(call(client, "kb_get_document", {"doc_id": "9-9"})["structuredContent"]) & OLD_KEYS
    assert (
        not _keys(call(client, "kb_check_citations", {"text": "ASC 9-9 pkt 2.3.2.4"})["structuredContent"]) & OLD_KEYS
    )
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert "kb_documents" in tools and "kb_sheets" not in tools
    schema = tools["kb_lookup"]["inputSchema"]
    assert "doc_id" in schema["properties"] and "doc_id" in schema["required"] and "ds" not in schema["properties"]
    assert "doc_id" in tools["kb_fulltext"]["inputSchema"]["properties"]
    assert "ds" not in tools["kb_search"]["inputSchema"]["properties"]
    for t in tools.values():  # no tool names an old key in what it takes or returns
        assert not _keys(t.get("inputSchema", {})) & OLD_KEYS and not _keys(t.get("outputSchema", {})) & OLD_KEYS, t[
            "name"
        ]
    out = tools["kb_lookup"]["outputSchema"]
    assert not _keys(out) & OLD_KEYS  # the schema of the answer, with $defs (Evidence ...), names no old key
    assert "doc_id" in out["properties"] and "document_indexed" in out["properties"]
    assert "doc_id" in out["$defs"]["EvidenceOut"]["properties"]
    assert "document_title" in out["$defs"]["EvidenceOut"]["properties"]


def test_old_argument_name_is_rejected(client):
    r = call(client, "kb_lookup", {"ds": "9-9", "ref": "2.3.2.4"})
    assert r["isError"] is True  # "doc_id" is required and "ds" is not accepted any more
