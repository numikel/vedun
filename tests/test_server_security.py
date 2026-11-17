"""Authentication, cross-site guard, rate limit, budget, audit log, check-citations scope. No network."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime

import pytest

from tests.helpers import fake_transport
from tests.helpers_server import (
    API_KEY,
    BASIC,
    BEARER,
    CSRF,
    DASH_PASSWORD,
    FakeSource,
    fake_catalog,
    use_source,
    wait_job,
)
from vedun import catalog as K
from vedun.server import api, audit, jobs, security
from vedun.state import connect_state

PROTECTED = [
    "/api/collection",
    "/api/stats",
    "/api/documents",
    "/",
    "/documents",
    "/sync",
    "/static/css/app.css",
    "/mcp",
]


# ------------------------------------------------------------------ authentication
@pytest.mark.parametrize("path", PROTECTED)
def test_no_credentials_is_401(raw_client, path):
    r = raw_client.get(path)
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    if path.startswith(("/api", "/mcp")):
        assert www == "Bearer"
    else:
        assert re.fullmatch(r'Basic realm="[^"]+", charset="UTF-8"', www), www


def test_wrong_credentials_are_401(raw_client):
    assert raw_client.get("/api/collection", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert raw_client.get("/api/collection", headers={"Authorization": f"Bearer {API_KEY}x"}).status_code == 401
    assert (
        raw_client.get("/", headers={"Authorization": "Basic " + "dGVzdGVyOm5vcGU="}).status_code == 401
    )  # tester:nope
    assert raw_client.get("/", headers={"Authorization": "Basic !!notbase64"}).status_code == 401
    assert raw_client.get("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_right_credentials_by_area(raw_client):
    assert raw_client.get("/api/collection", headers=BEARER).status_code == 200
    assert (
        raw_client.get("/api/collection", headers=BASIC).status_code == 200
    )  # the page's own fetch() uses cached Basic
    assert raw_client.get("/", headers=BASIC).status_code == 200
    assert raw_client.get("/static/css/app.css", headers=BASIC).status_code == 200
    assert raw_client.get("/", headers=BEARER).status_code == 401  # pages: Basic only
    assert raw_client.get("/mcp", headers=BASIC).status_code == 401  # MCP: Bearer only
    assert raw_client.get("/mcp", headers=BEARER).status_code == 405  # authenticated; MCP is POST-only


def test_health_is_open(raw_client):
    r = raw_client.get("/api/health")
    assert r.status_code == 200 and set(r.json()) == {"status", "version", "time"}


@pytest.mark.parametrize("unset", ["VEDUN_API_KEY", "VEDUN_DASH_USER", "VEDUN_DASH_PASSWORD"])
def test_unset_secret_closes_everything_with_503(raw_client, monkeypatch, unset):
    monkeypatch.delenv(unset)
    for path in ("/api/collection", "/", "/mcp"):
        r = raw_client.get(path, headers={**BEARER, **BASIC})
        assert r.status_code == 503, path
        assert unset in r.json()["error"]["message"]  # the name, never a value
    assert raw_client.get("/api/health").status_code == 200  # the healthcheck stays open


def test_startup_logs_one_clear_error_when_unset(server_kb, monkeypatch, caplog):
    from vedun.server.app import create_app

    monkeypatch.delenv("VEDUN_API_KEY")
    with caplog.at_level("ERROR", logger="vedun.server"):
        create_app()
    msgs = [r.getMessage() for r in caplog.records if r.name == "vedun.server"]
    assert len(msgs) == 1 and "VEDUN_API_KEY" in msgs[0] and "503" in msgs[0]


def test_dashboard_in_process_calls_work_without_external_credentials(raw_client):
    """/documents shows catalog-only rows only if the dashboard's own GET /api/collection got through the auth layer."""
    K.fetch_catalog(source=FakeSource())
    page = raw_client.get("/documents", headers=BASIC)
    assert page.status_code == 200 and "25-3" in page.text


def test_internal_token_is_not_guessable(raw_client):
    r = raw_client.get("/api/collection", headers={security.INTERNAL_HEADER: "guess"})
    assert r.status_code == 401


def test_secrets_never_appear_in_responses_or_audit(raw_client):
    raw_client.get("/api/collection", headers={"Authorization": f"Bearer {API_KEY}wrong"})
    time.sleep(0.01)
    blob = json.dumps(audit.read_tail(50))
    assert API_KEY not in blob and DASH_PASSWORD not in blob
    assert "wrong" not in blob


# ------------------------------------------------------------------ cross-site guard
def test_cross_site_post_is_403(raw_client):
    for extra in (
        {},  # no custom header
        {**CSRF, "Origin": "https://evil.example"},
        {**CSRF, "Sec-Fetch-Site": "cross-site"},
        {**CSRF, "Sec-Fetch-Site": "same-site"},
        {**CSRF, "Origin": "null"},
    ):
        r = raw_client.post("/api/jobs/catalog-refresh", headers={**BEARER, **extra})
        assert r.status_code == 403, extra
    for path, body in (
        ("/api/verify", {"doc_id": "9-9", "ref": "2.3.2.4", "quote": "x"}),
        ("/api/check-citations", {"text": "x"}),
        ("/api/jobs/check-citations", {}),
    ):
        assert raw_client.post(path, json=body, headers=BEARER).status_code == 403, path
    assert raw_client.post("/api/jobs/sync", headers=BASIC, data={"dry_run": "true"}).status_code == 403
    assert jobs.list_jobs() == []  # nothing was queued


def test_same_origin_post_passes(raw_client, monkeypatch):
    use_source(monkeypatch, FakeSource())
    ok = [
        {**BEARER, **CSRF},
        {**BEARER, "HX-Request": "true"},
        {**BASIC, **CSRF, "Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
        {**BASIC, **CSRF, "Origin": "http://localhost:8765", "Sec-Fetch-Site": "none"},
    ]
    for h in ok:
        r = raw_client.post("/api/jobs/catalog-refresh", headers=h)
        assert r.status_code == 202, h
        assert r.json()["id"] == r.json()["job_id"]  # the dashboard's upload script reads "id"


def test_paid_search_needs_the_same_guard_but_free_reads_do_not(raw_client):
    assert raw_client.get("/api/search", params={"q": "paint"}, headers=BEARER).status_code == 403
    assert raw_client.get("/api/search", params={"q": "paint"}, headers={**BEARER, **CSRF}).status_code == 200
    assert raw_client.get("/api/fulltext", params={"q": "paint"}, headers=BEARER).status_code == 200  # no cost
    assert raw_client.get("/api/pages/9-9/1.png", headers=BEARER).status_code in (200, 404)


def test_wrong_host_is_400(raw_client):
    assert raw_client.get("/api/health", headers={"host": "evil.example"}).status_code == 400


def test_dashboard_upload_path_still_works(raw_client):
    """What /sync's fetch() sends: cached Basic, X-Requested-With, multipart, Origin of the page."""
    h = {**BASIC, **CSRF, "Origin": "http://127.0.0.1:8765", "Accept": "application/json"}
    r = raw_client.post("/api/jobs/sync", headers=h, data={"fetch_from_source": "false", "dry_run": "true"})
    assert r.status_code == 202 and r.json()["id"]
    assert wait_job(_authed(raw_client), r.json()["id"])["status"] == "done"
    bad = raw_client.post("/api/jobs/sync", headers=h, files=[("files", ("x.pdf", b"%PDF-1", "application/pdf"))])
    assert bad.status_code == 400 and isinstance(bad.json()["detail"], str)  # the script shows data.detail


def _authed(c):
    c.headers.update({**BEARER, **CSRF})
    return c


# ------------------------------------------------------------------ rate limit
def test_rate_limit_returns_429_with_retry_after(raw_client, monkeypatch):
    monkeypatch.setenv("VEDUN_RATE_LIMIT_PER_MIN", "3")
    codes = [raw_client.get("/api/collection", headers=BEARER).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = raw_client.get("/api/collection", headers=BEARER)
    assert 1 <= int(r.headers["retry-after"]) <= 61
    assert raw_client.get("/api/collection", headers=BASIC).status_code == 200  # another credential, own window
    assert raw_client.get("/api/health").status_code == 200  # health is exempt
    assert raw_client.get("/mcp", headers=BEARER).status_code == 429  # /mcp shares the credential's window


# ------------------------------------------------------------------ budget
def _spend(usd: float) -> None:
    c = connect_state()
    c.execute(
        "INSERT OR REPLACE INTO budget_ledger(day, spent_usd) VALUES(?, ?)",
        (datetime.now(UTC).strftime("%Y-%m-%d"), usd),
    )
    c.close()


def test_exhausted_budget_makes_search_bm25_only_and_calls_nothing_external(client, monkeypatch):
    _spend(3.5)

    def boom(*a, **k):
        raise AssertionError("an external call was attempted with an exhausted budget")

    monkeypatch.setattr(api.hooks, "embedder_factory", boom)
    monkeypatch.setattr(api.hooks, "reranker_factory", boom)
    r = client.get("/api/search", params={"q": "45 kg paint per cabinet", "doc_id": "9-9"})
    assert r.status_code == 200
    body = r.json()
    assert body["budget_exhausted"] is True and body["hits"]
    assert any("daily API budget used up" in n for n in body["notes"])
    assert body["hits"][0]["retrieval"]["final_ranker"] == "bm25" and not body["hits"][0]["retrieval"]["vector"]
    assert body["rerank_error"] is None and body["reranked"] is False
    s = client.get("/api/stats").json()
    assert s["budget_usd"] == 3.0 and s["spent_today_usd"] == 3.5


def test_exhausted_budget_fails_sync_fast_but_allows_dry_run(client):
    _spend(3.0)
    j = wait_job(client, client.post("/api/jobs/sync").json()["job_id"])
    assert j["status"] == "failed" and "daily API budget used up" in j["error"]
    assert (
        wait_job(client, client.post("/api/jobs/sync", data={"dry_run": "true"}).json()["job_id"])["status"] == "done"
    )


def test_query_cost_is_accumulated_in_the_daily_ledger(client, monkeypatch):
    from vedun import config as C
    from vedun.embed import Embedder

    def priced(url, headers, body):
        status, payload = fake_transport(url, headers, body)
        payload["usage"]["cost"] = 0.25
        return status, payload

    monkeypatch.setattr(api.hooks, "embedder_factory", lambda k: Embedder(C.MODELS[k], transport=priced))
    for _ in range(2):
        assert client.get("/api/search", params={"q": "paint", "rerank": "none"}).status_code == 200
    assert client.get("/api/stats").json()["spent_today_usd"] == pytest.approx(0.5)


def test_budget_is_configurable(client, monkeypatch):
    monkeypatch.setenv("VEDUN_DAILY_BUDGET_USD", "0.1")
    _spend(0.2)
    assert client.get("/api/stats").json()["budget_usd"] == 0.1
    assert client.get("/api/search", params={"q": "paint"}).json()["budget_exhausted"] is True


# ------------------------------------------------------------------ audit log
def test_audit_log_records_actions_without_secrets_or_content(client, raw_client, monkeypatch):
    use_source(monkeypatch, FakeSource())
    jid = client.post("/api/jobs/catalog-refresh").json()["job_id"]
    assert wait_job(client, jid)["status"] == "done"
    client.get("/api/search", params={"q": "zzdistinctivequery"})
    raw_client.get("/api/collection", headers={"Authorization": "Bearer wrong-key-value"})
    raw_client.post("/api/jobs/catalog-refresh", headers={**BEARER, "X-Requested-With": ""})  # refused by the guard
    entries = audit.read_tail(100)
    mine = [e for e in entries if e.get("job_id") == jid]
    assert [(e["action"], e["outcome"], e["caller"]) for e in mine][:1] == [("catalog_refresh", "queued", "api")]
    assert any(e["action"] == "catalog_refresh" and e["outcome"] == "done" for e in mine)
    assert any(e["action"] == "auth_failure" and e["caller"] == "unknown" for e in entries)
    assert any(e["action"] == "csrf_denied" for e in entries)
    assert all({"ts", "action", "caller", "outcome"} <= set(e) for e in entries)
    raw = (audit.C.REPORT_DIR / "audit.jsonl").read_text(encoding="utf-8")
    for secret in (API_KEY, DASH_PASSWORD, "wrong-key-value", "zzdistinctivequery"):
        assert secret not in raw


def test_dashboard_submissions_are_attributed_to_the_dashboard(raw_client, monkeypatch):
    use_source(monkeypatch, FakeSource())
    jid = raw_client.post("/api/jobs/catalog-refresh", headers={**BASIC, **CSRF}).json()["job_id"]
    wait_job(_authed(raw_client), jid)
    assert any(e["job_id"] == jid and e["caller"] == "dashboard" for e in audit.read_tail(50))


def test_source_download_is_audited(client, monkeypatch):
    cat = fake_catalog("9-9")
    cat.entries["9-9"].edition = "2027-01"
    cat.entries["9-9"].published = "January 2027"
    use_source(monkeypatch, FakeSource(catalog=cat, pdf=b"<html>nope</html>"))
    jid = client.post("/api/jobs/sync", data={"fetch_from_source": "true"}).json()["job_id"]
    wait_job(client, jid)
    dl = [e for e in audit.read_tail(100) if e["action"] == "source_download" and e["job_id"] == jid]
    assert dl and dl[0]["outcome"] == "0 ok, 1 failed"


def test_old_fetch_field_name_is_not_recognised(client, monkeypatch):
    """``fetch_from_fm`` was renamed to ``fetch_from_source`` without an alias: the old field changes nothing."""
    use_source(monkeypatch, FakeSource(catalog=lambda: pytest.fail("the old field name must not reach the source")))
    jid = client.post("/api/jobs/sync", data={"fetch_from_fm": "true", "dry_run": "true"}).json()["job_id"]
    assert wait_job(client, jid)["status"] == "done"
    assert not [e for e in audit.read_tail(100) if e["action"] == "source_download" and e["job_id"] == jid]


# ------------------------------------------------------------------ check-citations scope
def test_check_citations_reads_only_small_md_and_txt_outside_skipped_dirs(client, server_kb):
    repo = server_kb.repo
    cite = "Wg ASC 9-9 pkt 2.3.2.4.\n"
    (repo / "ok.md").write_text(cite, encoding="utf-8")
    (repo / "ok.txt").write_text(cite, encoding="utf-8")
    (repo / "skip.docx").write_text(cite)
    (repo / "skip.py").write_text(cite)
    for d in (".git", ".venv", "node_modules"):
        (repo / d).mkdir()
        (repo / d / "inner.md").write_text(cite, encoding="utf-8")
    (repo / "huge.md").write_text(cite + "x" * 5_000_001, encoding="utf-8")  # > 5 MB: must not be read at all
    j = wait_job(client, client.post("/api/jobs/check-citations").json()["job_id"])
    assert j["status"] == "done" and j["result"]["n_files"] == 2 and j["result"]["n_citations"] == 2
    files = {r["file"] for r in client.get("/api/citations/latest").json()["results"]}
    assert files == {"ok.md", "ok.txt"}


# ------------------------------------------------------------------ X-API-Key (same key as Bearer)
def test_x_api_key_header_is_accepted_like_bearer(raw_client):
    good = {"X-API-Key": API_KEY}
    assert raw_client.get("/api/collection", headers=good).status_code == 200
    assert raw_client.get("/api/collection", headers={"X-API-Key": API_KEY + "x"}).status_code == 401
    assert raw_client.get("/api/collection", headers={"X-API-Key": ""}).status_code == 401
    r = raw_client.get("/api/collection", headers={"X-API-Key": "nope"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert raw_client.get("/mcp", headers=good).status_code in (404, 200, 307, 405)  # authenticated
    assert raw_client.get("/mcp", headers={"X-API-Key": "nope"}).status_code == 401
    assert raw_client.get("/", headers=good).status_code == 401  # dashboard pages stay Basic-only
    assert raw_client.post("/api/jobs/catalog-refresh", headers=good).status_code == 403  # still needs the CSRF header


# ------------------------------------------------------------------ job cancellation route
def _queued(job_id: str) -> str:
    """A queued job nobody will run (not in the worker's queue), so its status only changes through the route."""
    c = connect_state()
    c.execute(
        "INSERT INTO jobs(id, kind, params, status, message, created_at) "
        "VALUES(?, 'check_citations', '{}', 'queued', 'x', '2026-01-01T00:00:00+00:00')",
        (job_id,),
    )
    c.close()
    return job_id


def test_cancel_api_route_has_the_auth_and_cross_site_guards(raw_client):
    jid = _queued("cccccccccccc")
    url = f"/api/jobs/{jid}/cancel"
    assert raw_client.post(url, headers=CSRF).status_code == 401  # no credentials
    assert raw_client.post(url, headers={"Authorization": "Bearer nope", **CSRF}).status_code == 401
    assert raw_client.post(url, headers={"X-API-Key": "nope", **CSRF}).status_code == 401
    for extra in (
        {},  # credentials but no custom header
        {**CSRF, "Origin": "https://evil.example"},
        {**CSRF, "Sec-Fetch-Site": "cross-site"},
        {**CSRF, "Sec-Fetch-Site": "same-site"},
        {**CSRF, "Origin": "null"},
    ):
        assert raw_client.post(url, headers={**BEARER, **extra}).status_code == 403, extra
    assert raw_client.post(url, headers={"X-API-Key": API_KEY}).status_code == 403  # the key alone is not enough
    assert jobs.get_job(jid).status == "queued"  # none of the refused requests changed anything
    assert any(e["action"] == "csrf_denied" and url in e["outcome"] for e in audit.read_tail(50))
    r = raw_client.post(url, headers={"X-API-Key": API_KEY, **CSRF})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert any(e["action"] == "job_cancel" and e["job_id"] == jid and e["caller"] == "api" for e in audit.read_tail(50))


def test_cancel_api_route_accepts_the_dashboards_own_credentials(raw_client):
    jid = _queued("dddddddddddd")
    r = raw_client.post(
        f"/api/jobs/{jid}/cancel",
        headers={**BASIC, **CSRF, "Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200 and jobs.get_job(jid).status == "cancelled"


def test_cancel_dashboard_route_needs_basic_auth_and_htmx(raw_client):
    jid = _queued("eeeeeeeeeeee")
    url = f"/sync/jobs/{jid}/cancel"
    assert raw_client.post(url, headers={"HX-Request": "true"}).status_code == 401  # no credentials
    assert raw_client.post(url, headers={**BEARER, "HX-Request": "true"}).status_code == 401  # pages: Basic only
    assert raw_client.post(url, headers=BASIC).status_code == 400  # a plain cross-site form post carries no HX header
    assert raw_client.post(url, headers={**BASIC, **CSRF}).status_code == 400  # X-Requested-With is the API's header
    assert jobs.get_job(jid).status == "queued"
    r = raw_client.post(url, headers={**BASIC, "HX-Request": "true"})
    assert r.status_code == 200 and "Anulowane" in r.text and jobs.get_job(jid).status == "cancelled"
    assert any(
        e["action"] == "job_cancel" and e["job_id"] == jid and e["caller"] == "dashboard" for e in audit.read_tail(50)
    )
    again = raw_client.post(url, headers={**BASIC, "HX-Request": "true"})  # finished: a message beside the button
    assert again.status_code == 200 and "już się zakończyło" in again.text
    assert again.headers["HX-Retarget"] == "#cancel-msg"
