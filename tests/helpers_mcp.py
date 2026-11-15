"""Raw JSON-RPC over the real app for the MCP tests (protocol revision 2026-07-28, plus the legacy handshake)."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
MCP_KEY = {"X-API-Key": "test-api-key-not-real"}


def rpc(
    client: Any,
    method: str,
    params: dict | None = None,
    *,
    path: str = "/mcp",
    headers: dict | None = None,
    protocol: str | None = PROTOCOL,
    id_: int = 1,
    auth: dict | None = MCP_KEY,
) -> Any:
    """POST one modern-era JSON-RPC request (the HTTP response object)."""
    p = dict(params or {})
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **(auth or {})}
    if protocol:
        p["_meta"] = {**META, "io.modelcontextprotocol/protocolVersion": protocol}
        h["MCP-Protocol-Version"] = protocol
        h["Mcp-Method"] = method
        if "name" in p and method in ("tools/call", "prompts/get"):
            h["Mcp-Name"] = p["name"]
    h.update(headers or {})
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": p}
    return client.post(path, headers=h, content=json.dumps(body), follow_redirects=False)


def call(client: Any, name: str, arguments: dict | None = None, **kw: Any) -> dict:
    """``tools/call``; returns the JSON-RPC ``result`` (raises with the body on a protocol error)."""
    r = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kw)
    assert r.status_code == 200, (r.status_code, r.text[:500])
    body = r.json()
    assert "result" in body, body
    return body["result"]


def text_of(result: dict) -> str:
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")
