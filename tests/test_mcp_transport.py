"""MCP transport, discovery, versions and authentication of ``/mcp`` on the real app (revision 2026-07-28 + legacy leg)."""

from __future__ import annotations

import json

from tests.helpers_mcp import MCP_KEY, call, rpc, text_of
from tests.helpers_server import API_KEY, BASIC, BEARER
from vedun import config as C

TOOLS = [
    "kb_lookup",
    "kb_toc",
    "kb_fulltext",
    "kb_search",
    "kb_get_document",
    "kb_figure",
    "kb_page_image",
    "kb_verify_quote",
    "kb_documents",
    "kb_check_citations",
]


# ============================================================================ discovery
def test_server_discover(raw_client):
    r = rpc(raw_client, "server/discover")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    res = r.json()["result"]
    assert res["resultType"] == "complete" and "2026-07-28" in res["supportedVersions"]
    info = res["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["name"] == "vedun" and info["version"]
    assert "tools" in res["capabilities"] and res["capabilities"]["tools"].get("listChanged") is False
    assert "subscriptions" not in json.dumps(res["capabilities"]).lower()  # nothing to listen to
    for tool in TOOLS:
        assert tool in res["instructions"], tool
    assert "citation_pl" in res["instructions"] and "next_cursor" in res["instructions"]
    assert res["ttlMs"] > 0 and res["cacheScope"] == "public"  # caching hints are mandatory for discover


def test_tools_list_is_deterministic_annotated_and_cacheable(raw_client):
    a = rpc(raw_client, "tools/list").json()["result"]
    b = rpc(raw_client, "tools/list").json()["result"]
    assert [t["name"] for t in a["tools"]] == TOOLS == [t["name"] for t in b["tools"]]
    assert a["ttlMs"] > 0 and a["cacheScope"] == "public" and "nextCursor" not in a
    for t in a["tools"]:
        ann = t["annotations"]
        assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False and ann["title"]
        assert ann["openWorldHint"] is (t["name"] == "kb_search")  # only the search calls external APIs
        assert t["inputSchema"]["type"] == "object" and len(t["description"]) > 200
        assert len(t["name"]) <= 64 and t["name"].replace("_", "").isalnum()
    d = {t["name"]: t["description"] for t in a["tools"]}
    # the descriptions tell which tool to use instead (sibling disambiguation) and where citations come from
    assert "kb_search" in d["kb_lookup"] and "kb_fulltext" in d["kb_lookup"] and "citation_pl" in d["kb_lookup"]
    assert "kb_verify_quote" in d["kb_lookup"] and "kb_verify_quote" in d["kb_search"]
    assert "kb_lookup" in d["kb_fulltext"] and "kb_search" in d["kb_fulltext"] and "no paid API" in d["kb_fulltext"]
    assert "budget" in d["kb_search"] and "BM25" in d["kb_search"]
    assert "never returned" in d["kb_get_document"] or "never" in d["kb_get_document"]
    # the request carried no extra transport header requirements on tool arguments (no x-mcp-header annotations)
    assert "x-mcp-header" not in json.dumps(a)


# ============================================================================ authentication
def test_mcp_requires_the_api_key(raw_client):
    for path in ("/mcp", "/mcp/"):
        r = rpc(raw_client, "tools/list", path=path, auth=None)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", path
        assert r.headers["www-authenticate"] == "Bearer"
        assert rpc(raw_client, "tools/list", path=path, auth={"X-API-Key": "nope"}).status_code == 401
        assert rpc(raw_client, "tools/list", path=path, auth={"Authorization": "Bearer nope"}).status_code == 401
        assert rpc(raw_client, "tools/list", path=path, auth=BASIC).status_code == 401  # dashboard credentials: no
        assert rpc(raw_client, "tools/list", path=path, auth=MCP_KEY).status_code == 200
        assert rpc(raw_client, "tools/list", path=path, auth=BEARER).status_code == 200
    assert API_KEY in MCP_KEY.values()


def test_unauthenticated_tool_call_executes_nothing(raw_client, monkeypatch):
    from vedun.server import mcp_tools

    def boom(*a, **k):
        raise AssertionError("tool body reached without authentication")

    monkeypatch.setattr(mcp_tools.kb, "lookup", boom)
    r = rpc(raw_client, "tools/call", {"name": "kb_lookup", "arguments": {"doc_id": "9-9", "ref": "1"}}, auth=None)
    assert r.status_code == 401


def test_mcp_fails_closed_without_configured_secrets(server_kb, monkeypatch):
    from fastapi.testclient import TestClient

    from vedun.server.app import create_app

    monkeypatch.delenv("VEDUN_API_KEY")
    with TestClient(create_app()) as c:
        r = rpc(c, "tools/list")
        assert r.status_code == 503 and r.json()["error"]["code"] == "auth_not_configured"


# ============================================================================ transport rules of the 2026-07-28 revision
def test_exact_path_has_no_redirect_and_both_forms_work(raw_client):
    for path in ("/mcp", "/mcp/"):
        r = rpc(raw_client, "tools/list", path=path)
        assert r.status_code == 200 and "location" not in r.headers, path


def test_only_post_is_served_no_get_stream_no_session(raw_client):
    for method in ("GET", "DELETE", "PUT", "HEAD", "PATCH"):
        for path in ("/mcp", "/mcp/"):
            r = raw_client.request(method, path, headers={**MCP_KEY, "Accept": "text/event-stream"})
            assert r.status_code == 405 and r.headers["allow"] == "POST", (method, path)
    r = rpc(raw_client, "tools/list", headers={"Mcp-Session-Id": "abc", "Last-Event-ID": "5"})
    assert r.status_code == 200 and "mcp-session-id" not in r.headers  # sent ids are ignored, none is minted


def test_header_validation_follows_the_spec(raw_client):
    # Mcp-Name must equal params.name
    r = rpc(raw_client, "tools/call", {"name": "kb_documents", "arguments": {}}, headers={"Mcp-Name": "kb_toc"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    # a required standard header is missing
    r = rpc(raw_client, "tools/call", {"name": "kb_documents", "arguments": {}}, headers={"Mcp-Method": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    # unsupported version: 400 + the supported list
    r = rpc(raw_client, "tools/list", protocol="2030-01-01")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
    assert r.json()["error"]["data"]["supported"] == ["2026-07-28"]
    # unknown method on the modern leg
    r = rpc(raw_client, "nope/method")
    assert r.status_code in (400, 404) and r.json()["error"]["code"] == -32601


def test_malformed_or_oversized_requests_are_rejected_before_any_tool_runs(raw_client):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **MCP_KEY}
    assert raw_client.post("/mcp", headers=h, content="{not json").status_code == 400
    assert raw_client.post("/mcp", headers={**h, "Content-Type": "text/plain"}, content="{}").status_code == 400
    big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pad": "x" * 300_000}})
    r = raw_client.post(
        "/mcp", headers={**h, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}, content=big
    )
    assert r.status_code == 413


def test_dns_rebinding_guard_checks_origin_and_host(raw_client):
    assert rpc(raw_client, "tools/list", headers={"Origin": "http://evil.example"}).status_code == 403  # spec MUST
    assert rpc(raw_client, "tools/list", headers={"Origin": "https://127.0.0.1:8765"}).status_code == 403
    assert rpc(raw_client, "tools/list", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert rpc(raw_client, "tools/list", headers={"Origin": "http://localhost:8765"}).status_code == 200
    assert rpc(raw_client, "tools/list", headers={"Host": "evil.example"}).status_code in (400, 421)
    assert rpc(raw_client, "tools/list", headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_rate_limit_applies_to_mcp(server_kb, monkeypatch):
    from fastapi.testclient import TestClient

    from vedun.server.app import create_app

    monkeypatch.setenv("VEDUN_RATE_LIMIT_PER_MIN", "3")
    with TestClient(create_app()) as c:
        codes = [rpc(c, "tools/list").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


# ============================================================================ handshake-era clients
def _legacy(c, method, params, id_):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **MCP_KEY}
    if method != "initialize":
        h["MCP-Protocol-Version"] = "2025-11-25"
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
    return c.post("/mcp", headers=h, content=json.dumps(body))


def test_handshake_era_client_is_served_statelessly(raw_client):
    init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}
    r = _legacy(raw_client, "initialize", init, 1)
    assert r.status_code == 200 and "mcp-session-id" not in r.headers  # stateless: no session to lose on restart
    res = r.json()["result"]
    assert (
        res["protocolVersion"] == "2025-11-25"
        and res["serverInfo"]["name"] == "vedun"
        and "tools" in res["capabilities"]
    )
    r = _legacy(raw_client, "tools/list", {}, 2)
    assert [t["name"] for t in r.json()["result"]["tools"]] == TOOLS
    r = _legacy(raw_client, "tools/call", {"name": "kb_lookup", "arguments": {"doc_id": "9-9", "ref": "2.3.2.4"}}, 3)
    res = r.json()["result"]
    assert (
        res["isError"] is False
        and "45 kg (99 lb)" in res["content"][0]["text"]
        and res["structuredContent"]["all_found"]
    )


# ============================================================================ failure modes
def test_missing_database_is_a_clean_tool_error_without_paths(client, monkeypatch, tmp_path):
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "gone.db")
    r = call(client, "kb_lookup", {"doc_id": "9-9", "ref": "1"})
    assert r["isError"] is True and "database unavailable" in text_of(r)
    assert "gone.db" not in text_of(r) and str(tmp_path) not in text_of(r)


def test_unexpected_exception_does_not_leak_internals(client, monkeypatch):
    from vedun.server import mcp_tools

    def boom(*a, **k):
        raise RuntimeError("internal secret detail C:/secret/path")

    monkeypatch.setattr(mcp_tools.kb, "toc", boom)
    r = call(client, "kb_toc", {"doc_id": "9-9"})
    assert r["isError"] is True and "secret" not in text_of(r)
