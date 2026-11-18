"""Dashboard-issued API keys: scopes, expiry, revocation, the value never stored, every API route classified."""

from __future__ import annotations

import re

import pytest

from tests.helpers_server import BASIC, CSRF
from vedun.server import api, keys
from vedun.state import connect_state, state_path


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ store
def test_value_is_shown_once_and_only_its_hash_is_stored(server_kb):
    rec, token = keys.create("Cursor", {"mcp:read"}, 90)
    assert token.startswith("vk_") and len(token) > 40 and rec.prefix == token[:10]
    assert rec.scopes == {"mcp:read"} and rec.status == "active" and rec.expires_at
    raw = state_path().read_bytes()
    for f in state_path().parent.glob("state.db*"):
        raw += f.read_bytes()
    assert token.encode() not in raw  # the value is not in the database, its WAL or its journal
    assert keys.verify(token).name == "Cursor" and keys.list_keys()[0].last_used_at  # use is recorded
    assert keys.verify(token + "x") is None and keys.verify("not-a-key") is None


def test_scopes_are_normalized_and_validated(server_kb):
    rec, _ = keys.create("  scripts\n  nightly ", ["api:write", "bogus"], None)
    assert rec.name == "scripts nightly" and rec.scopes == {"api:read", "api:write"} and rec.expires_at is None
    with pytest.raises(ValueError):
        keys.create("x", ["bogus"], 90)
    with pytest.raises(ValueError):
        keys.create("   ", ["mcp:read"], 90)
    with pytest.raises(ValueError):
        keys.create("x", ["mcp:read"], 7)  # only the offered expiries


def test_revoked_and_expired_keys_stop_working(server_kb):
    rec, token = keys.create("old", {"mcp:read"}, 30)
    assert keys.revoke(rec.id) and not keys.revoke(rec.id)  # twice is harmless
    assert keys.verify(token) is None
    rec2, token2 = keys.create("expiring", {"mcp:read"}, 30)
    conn = connect_state()
    conn.execute("UPDATE api_keys SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (rec2.id,))
    conn.close()
    assert keys.verify(token2) is None
    assert {k.name: k.status for k in keys.list_keys()} == {"old": "revoked", "expiring": "expired"}


# ------------------------------------------------------------------ HTTP: scopes
def test_mcp_key_opens_only_mcp(raw_client):
    _, token = keys.create("agent", {"mcp:read"}, 90)
    assert raw_client.get("/mcp", headers=bearer(token)).status_code == 405  # authenticated; MCP is POST-only
    r = raw_client.get("/api/collection", headers=bearer(token))
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"


def test_read_key_reads_but_cannot_start_jobs(raw_client):
    _, token = keys.create("reader", {"api:read"}, 90)
    h = {**bearer(token), **CSRF}
    assert raw_client.get("/api/collection", headers=h).status_code == 200
    assert raw_client.post("/api/check-citations", headers=h, json={"text": "nothing"}).status_code == 200
    assert raw_client.post("/api/jobs/nope/cancel", headers=h).status_code == 403
    assert raw_client.get("/mcp", headers=h).status_code == 403
    assert raw_client.get("/api/collection", headers={"X-API-Key": token}).status_code == 200  # same key, other header


def test_write_key_passes_the_scope_check(raw_client):
    _, token = keys.create("ops", {"api:write"}, 90)
    r = raw_client.post("/api/jobs/nope/cancel", headers={**bearer(token), **CSRF})
    assert r.status_code not in (401, 403, 503)  # past authentication and scope; the id itself is rejected later


def test_revoked_key_is_401(raw_client):
    rec, token = keys.create("gone", {"api:read"}, 90)
    keys.revoke(rec.id)
    assert raw_client.get("/api/collection", headers=bearer(token)).status_code == 401


def test_keys_never_manage_keys_and_dashboard_login_is_needed(raw_client):
    _, token = keys.create("ops", {"api:write"}, 90)
    r = raw_client.get("/connect", headers=bearer(token))
    assert r.status_code == 401  # pages, key management included, are HTTP Basic only


# ------------------------------------------------------------------ fail closed without a master key
def test_without_master_key_dashboard_works_and_api_waits_for_a_key(raw_client, monkeypatch):
    monkeypatch.delenv("VEDUN_API_KEY")
    assert raw_client.get("/", headers=BASIC).status_code == 200  # log in and create a key
    for path in ("/api/collection", "/mcp"):
        r = raw_client.get(path, headers=bearer("anything"))
        assert r.status_code == 503 and "MCP connection" in r.json()["error"]["message"], path
    _, token = keys.create("agent", {"mcp:read"}, 90)
    assert raw_client.get("/mcp", headers=bearer(token)).status_code == 405
    assert raw_client.get("/mcp", headers=bearer("vk_wrong")).status_code == 401  # a key exists: wrong one is 401
    assert raw_client.get("/api/collection", headers=bearer(token)).status_code == 403  # known key, no api scope
    assert raw_client.get("/api/collection", headers=bearer("vk_wrong")).status_code == 503  # no api:* key at all
    assert raw_client.get("/api/collection", headers=BASIC).status_code == 200  # the dashboard's own calls


# ------------------------------------------------------------------ every route is classified
def test_every_api_route_has_a_scope():
    """A new state-changing route must be listed in keys.READ_POSTS or keys.WRITE_ROUTES on purpose."""
    unclassified = []
    for r in api.router.routes:
        for method in getattr(r, "methods", set()) - {"GET", "HEAD", "OPTIONS"}:
            sample = re.sub(r"\{[^}]+\}", "x", r.path)
            listed = sample in keys.READ_POSTS or any(
                m == method and rx.fullmatch(sample) for m, rx in keys.WRITE_ROUTES
            )
            if not listed:
                unclassified.append(f"{method} {r.path}")
    assert not unclassified, unclassified
    assert keys.required_scope("api", "POST", "/api/verify") == "api:read"
    assert keys.required_scope("api", "POST", "/api/jobs/sync") == "api:write"
    assert keys.required_scope("api", "POST", "/api/some-future-route") == "api:write"  # unknown = strict


# ------------------------------------------------------------------ dashboard page /connect
HX = {"HX-Request": "true"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def test_connect_page_offers_keys_but_never_prints_a_value(raw_client):
    from tests.helpers_server import API_KEY

    html = raw_client.get("/connect?lang=en", headers=BASIC).text
    assert "Keys for clients" in html and "Show the key" in html and "MCP is off" not in html
    assert API_KEY not in html  # the master key comes only from POST /connect/key


def test_master_key_is_shown_on_request_only(raw_client):
    from tests.helpers_server import API_KEY

    assert raw_client.post("/connect/key", headers=BASIC).status_code == 400  # HTMX header required (CSRF)
    r = raw_client.post("/connect/key", headers={**BASIC, **HX})
    assert r.status_code == 200 and API_KEY in r.text and r.headers["cache-control"] == "no-store"
    assert "/connect/key/hidden" in r.text  # hidden again after 60 s
    assert raw_client.post("/connect/key", headers={**bearer("vk_anything"), **HX}).status_code == 401  # Basic only


def test_create_show_once_and_revoke_on_the_dashboard(raw_client):
    h = {**BASIC, **HX, **FORM}
    r = raw_client.post("/connect/keys?lang=en", headers=h, content="name=Cursor&scopes=mcp%3Aread&expiry=30")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    token = re.search(r"vk_[A-Za-z0-9_-]{40,}", r.text).group(0)
    assert raw_client.get("/mcp", headers=bearer(token)).status_code == 405  # works at once
    again = raw_client.get("/connect?lang=en", headers=BASIC).text
    assert "Cursor" in again and token not in again and token[:10] in again  # shown once, prefix stays
    key_id = keys.list_keys()[0].id
    r = raw_client.post(f"/connect/keys/{key_id}/revoke?lang=en", headers={**BASIC, **HX})
    assert r.status_code == 200 and "revoked" in r.text
    assert raw_client.get("/mcp", headers=bearer(token)).status_code == 401  # the master key still opens /mcp


def test_create_without_a_permission_explains_why(raw_client):
    h = {**BASIC, **HX, **FORM}
    r = raw_client.post("/connect/keys?lang=en", headers=h, content="name=x&expiry=90")
    assert r.status_code == 200 and "at least one permission" in r.text and "vk_" not in r.text


def test_banner_when_mcp_has_no_key(raw_client, monkeypatch):
    monkeypatch.delenv("VEDUN_API_KEY")
    html = raw_client.get("/connect?lang=en", headers=BASIC).text
    assert "MCP is off: there is no key." in html and "Show the key" not in html
    keys.create("agent", {"mcp:read"}, 90)
    assert "MCP is off" not in raw_client.get("/connect?lang=en", headers=BASIC).text
