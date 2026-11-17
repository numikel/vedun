"""REST API: shapes the dashboard relies on, retrieval endpoints, error envelope, hardening. No network."""

from __future__ import annotations

import pytest

from tests.helpers_server import BASIC, FakeSource
from vedun import catalog as K


def test_health_and_stats(client):
    h = client.get("/api/health").json()
    assert set(h) == {"status", "version", "time"} and h["status"] == "ok"  # nothing else is public
    s = client.get("/api/stats").json()
    assert s["corpus"]["documents"] == 1 and s["corpus"]["units"] > 5
    assert (s["retrieval"]["w_fts"], s["retrieval"]["w_vec"]) == (0.4, 0.6)
    assert s["retrieval"]["default_text_model"] == "i1" and s["retrieval"]["reranker"] == "r3"
    assert s["catalog"] == {} and "cost_usd" in s and "last_sync" in s
    assert s["budget_usd"] == 3.0 and s["spent_today_usd"] == 0.0


def test_collection_lists_indexed_and_catalog_entries(client, server_kb, tmp_path):
    body = client.get("/api/collection").json()
    assert [s["doc_id"] for s in body["documents"]] == ["9-9"]
    assert body["documents"][0]["indexed"] and body["documents"][0]["status"] == "unknown"  # no catalog fetched yet
    K.fetch_catalog(source=FakeSource())
    body = client.get("/api/collection").json()
    by = {s["doc_id"]: s for s in body["documents"]}
    assert by["9-9"]["catalog_status"] == "current" and by["9-9"]["source_item_id"]
    assert by["25-3"]["catalog_status"] == "missing_locally" and not by["25-3"]["indexed"]
    assert body["counts"]["current"] == 1 and body["total"] == len(body["documents"]) == 6
    assert [
        s["doc_id"]
        for s in client.get("/api/collection", params={"status": "missing_locally", "q": "plant"}).json()["documents"]
    ] == ["25-3"]
    assert client.get("/api/stats").json()["catalog"]["missing_locally"] == 5
    one = client.get("/api/collection/9-9").json()
    assert one["kinds"]["clause"] > 0 and one["pages"] == 3


def test_lookup_and_evidence_links(client):
    r = client.get(
        "/api/lookup", params=[("doc_id", "9-9"), ("ref", "pkt 2.3.2.4"), ("ref", "2.9.9.9"), ("ref", "rys. 2.3.1")]
    ).json()
    assert r["found"] is False  # one of the three is unknown
    a, b, f = r["items"]
    assert (
        a["found"] and a["evidence"]["citation_pl"].endswith("pkt 2.3.2.4") and "45 kg (99 lb)" in a["evidence"]["text"]
    )
    assert not b["found"] and b["nearest"]
    assert f["found"] and f["evidence"]["figure_url"] == "/api/figures/9-9/Figure%202.3.1.png"
    assert (
        a["evidence"]["source_pdf"].replace("\\", "/").endswith("pdf/9-9_202610.pdf")
    )  # derived from the configured PDF dir
    assert client.get(f["evidence"]["figure_url"]).headers["content-type"] == "image/png"
    assert client.get("/api/lookup", params={"doc_id": "8-8", "ref": "1.1"}).json()["document_indexed"] is False


def test_toc_and_unknown_sheet(client):
    t = client.get("/api/toc/9-9", params={"prefix": "2.3", "depth": 1}).json()
    assert t["found"] and {i["unit_id"] for i in t["items"]} >= {"2.3.2.1"} and all(i["depth"] <= 1 for i in t["items"])
    e = client.get("/api/toc/8-8")
    assert e.status_code == 404 and e.json()["error"]["code"] == "not_found"


def test_fulltext_modes_and_bad_expression(client):
    ok = client.get("/api/fulltext", params={"q": '"45 kg"'}).json()
    assert ok["ok"] and ok["hits"][0]["unit_id"] == "2.3.2.4" and ok["hits"][0]["snippet"]
    bad = client.get("/api/fulltext", params={"q": '"unbalanced', "syntax": "fts5"})
    assert (
        bad.status_code == 400
        and bad.json()["error"]["code"] == "invalid_query"
        and "FTS5" in bad.json()["error"]["message"]
    )
    assert client.get("/api/fulltext", params={"q": "kg", "kind": "table"}).json()["total"] == 0


def test_search_without_typesafe_key_returns_unreranked_hits(client):
    r = client.get("/api/search", params={"q": "45 kg paint per cabinet", "doc_id": "9-9"})
    assert r.status_code == 200
    body = r.json()
    assert body["hits"] and body["reranker"] == "r3" and body["reranked"] is False
    assert body["rerank_error"] and "Jev" in body["rerank_error"]
    assert (body["w_fts"], body["w_vec"]) == (0.4, 0.6)  # server defaults
    assert body["hits"][0]["retrieval"]["final_ranker"] == "wrrf" and "page_url" in body["hits"][0]
    plain = client.get("/api/search", params={"q": "45 kg", "rerank": "none", "w_fts": 1, "w_vec": 0}).json()
    assert plain["rerank_error"] is None and plain["reranker"] is None and plain["w_vec"] == 0


def test_search_uses_a_working_reranker(client, monkeypatch):
    from vedun.server import api

    class Flip:
        key = "r3"

        def rerank(self, query, documents):
            return [float(i) for i, _ in enumerate(documents)]  # reverses the fused order

    monkeypatch.setattr(api.hooks, "reranker_factory", lambda k: Flip())
    body = client.get("/api/search", params={"q": "paint", "doc_id": "9-9"}).json()
    assert body["reranked"] is True and body["rerank_error"] is None
    assert body["hits"][0]["retrieval"]["final_ranker"] == "wrrf+rerank:r3"


def test_verify_and_check_citations(client):
    v = client.post(
        "/api/verify", json={"doc_id": "9-9", "ref": "2.3.2.4", "quote": "45 kg (99 lb) per cabinet"}
    ).json()
    assert v["in_unit"] is True and v["evidence"]["unit_id"] == "2.3.2.4"
    c = client.post("/api/check-citations", json={"text": "ASC 9-9 pkt 2.3.2.4 and ASC 9-9 pkt 7.7.7"}).json()
    assert c["counts"] == {"OK": 1, "UNKNOWN UNIT": 1, "DOCUMENT NOT INDEXED": 0}


def test_validation_and_error_envelope(client):
    r = client.get("/api/search", params={"q": "x", "limit": 999})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error" and r.json()["error"]["details"]
    assert client.get("/api/fulltext", params={"q": "x", "doc_id": "../etc"}).status_code == 422
    assert client.get("/api/toc/not-a-sheet").status_code == 422
    assert client.post("/api/verify", json={"doc_id": "9-9", "ref": "", "quote": "x"}).status_code == 422
    assert client.get("/api/nope").json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("name", ["..%2F..%2Fsecret.md", "..\\x.md", "a%00.md", ".hidden.md", "x.py"])
def test_report_names_cannot_escape(client, server_kb, name):
    (server_kb.root / "reports").mkdir()
    (server_kb.root / "secret.md").write_text("secret")
    assert client.get(f"/api/reports/{name}").status_code in (404, 422)


def test_reports_listing(client, server_kb):
    rd = server_kb.root / "reports"
    rd.mkdir()
    (rd / "sync_2026-11-10.md").write_text("# report", encoding="utf-8")
    (rd / "notes.txt").write_text("ignored")
    assert [r["name"] for r in client.get("/api/reports").json()["reports"]] == ["sync_2026-11-10.md"]
    assert client.get("/api/reports/sync_2026-11-10.md").text == "# report"


def test_locked_database_answers_503_with_retry_after(client, monkeypatch):
    import sqlite3

    from vedun.server import settings as S

    class FailingConn:  # the lock hits the first query, after the connection opened fine
        def __init__(self, msg):
            self.msg = msg

        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError(self.msg)

        def close(self):
            pass

    monkeypatch.setattr(S, "open_readonly", lambda: FailingConn("database is locked"))
    r = client.get("/api/collection")
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert r.json()["error"]["code"] == "unavailable" and "busy" in r.json()["error"]["message"]

    monkeypatch.setattr(S, "open_readonly", lambda: FailingConn("no such table: x"))
    assert client.get("/api/collection").status_code == 500  # other SQLite errors stay internal errors


def test_host_header_is_checked(client):
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400


def test_dashboard_is_mounted_if_present(client):
    r = client.get("/", headers=BASIC)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_connect_page_shows_endpoint_tools_and_snippets_without_the_key(client):
    from tests.helpers_server import API_KEY

    r = client.get("/connect", headers=BASIC)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert 'href="/connect"' in html and "Połączenie MCP" in html
    assert "/mcp" in html and "kb_lookup" in html and "kb_check_citations" in html
    for client_name in ("Claude Code", "Cursor", "VS Code", "Claude Desktop"):
        assert client_name in html
    assert API_KEY not in html  # snippets reference ${VEDUN_API_KEY}, never the value
    assert "${VEDUN_API_KEY}" in html


def test_read_connections_are_memory_mapped(server_kb, monkeypatch):
    """Vector search through a Docker bind mount needs mmap (25-30 s per query without it)."""
    from vedun.server import settings as S

    with S.open_readonly() as c:
        assert c.execute("PRAGMA mmap_size").fetchone()[0] == 1024 * 1024 * 1024
    monkeypatch.setenv("VEDUN_MMAP_MB", "0")
    with S.open_readonly() as c:
        assert c.execute("PRAGMA mmap_size").fetchone()[0] == 0


OLD_KEYS = {"ds", "sheet_title", "fm_item_id", "sheet_indexed", "sheets"}


def _keys(o):
    """Every dict key in a JSON-like structure."""
    if isinstance(o, dict):
        return set(o) | {k for v in o.values() for k in _keys(v)}
    if isinstance(o, list):
        return {k for v in o for k in _keys(v)}
    return set()


def test_answers_carry_document_names_only(client):
    lookup = client.get("/api/lookup", params={"doc_id": "9-9", "ref": "2.3.2.4"}).json()
    ev = lookup["items"][0]["evidence"]
    assert lookup["doc_id"] == ev["doc_id"] == "9-9" and ev["document_title"] and "source_item_id" in ev
    assert lookup["document_indexed"] is True and not _keys(lookup) & OLD_KEYS
    coll = client.get("/api/collection").json()
    assert coll["documents"][0]["doc_id"] == "9-9" and not _keys(coll) & OLD_KEYS
    assert not _keys(client.get("/api/collection/9-9").json()) & OLD_KEYS
    stats = client.get("/api/stats").json()
    assert stats["corpus"]["documents"] == 1 and not _keys(stats) & OLD_KEYS
    hits = client.get("/api/fulltext", params={"q": "paint", "doc_id": "9-9"}).json()["hits"]
    assert hits and all(h["doc_id"] == "9-9" for h in hits)
    assert not _keys(client.get("/api/toc/9-9").json()) & OLD_KEYS
    assert not _keys(client.get("/api/documents").json()) & OLD_KEYS
    assert not _keys(client.get("/api/documents/9-9").json()) & OLD_KEYS
    assert (
        not _keys(client.post("/api/check-citations", json={"text": "ASC 9-9 pkt 2.3.2.4"}, headers=BASIC).json())
        & OLD_KEYS
    )


def test_pre_rename_names_are_gone(client):
    assert client.get("/api/sheets").status_code == 404
    assert client.get("/api/sheets/9-9").status_code == 404
    # the old parameter name is simply not known: a required parameter is missing (422), an optional one is ignored
    assert client.get("/api/lookup", params={"ds": "9-9", "ref": "2.3.2.4"}).status_code == 422
    assert client.get("/api/fulltext", params={"q": "paint", "ds": "../etc"}).status_code == 200
    assert (
        client.post("/api/verify", json={"ds": "9-9", "ref": "2.3.2.4", "quote": "x"}, headers=BASIC).status_code == 422
    )
    spec = client.get("/api/openapi.json").json()["paths"]
    assert "/api/sheets" not in spec and "/api/sheets/{ds}" not in spec and "/api/collection/{doc_id}" in spec
    assert [p["name"] for p in spec["/api/lookup"]["get"]["parameters"]][0] == "doc_id"


def test_fresh_install_without_a_database_turns_healthy(server_kb, tmp_path, monkeypatch):
    """First start of a new install: no vedun.db yet. The service creates an empty one instead of answering 503."""
    from fastapi.testclient import TestClient

    from tests.helpers_server import BEARER
    from vedun import config as C
    from vedun.server.app import create_app

    fresh = tmp_path / "fresh" / "vedun.db"
    monkeypatch.setattr(C, "DB_PATH", fresh)
    with TestClient(create_app()) as c:
        assert c.get("/api/health").status_code == 200 and fresh.exists()
        assert c.get("/api/stats", headers=BEARER).json()["corpus"]["documents"] == 0
