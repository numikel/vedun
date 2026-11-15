"""Web UI tests: every page renders on a synthetic DB, search tabs, verify-quote, XSS, optional integrations."""

from __future__ import annotations

import re
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from tests.helpers import BODY_V1, fake_converter, fake_embedders, make_pdf
from vedun import config as C
from vedun import db as D
from vedun.web import STATIC_DIR, router
from vedun.web import data as W

HX = {"HX-Request": "true"}


def plain(html: str) -> str:
    """Visible text of a page: tags dropped, whitespace (including no-break spaces) collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


REPORT = (
    "# Sync report 2026-11-10 23:14\n\nAdded: 9-9; replaced: -; removed (PDF gone): -; unchanged: 0.\n\n"
    "## DS 9-9: new sheet, edition 2026-10 (10 units, 12 chunks)\n"
)


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """A tiny file DB with one sheet (ASC 9-9, edition 2026-10) the web layer opens through ``C.DB_PATH``."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-real-key")  # the fake transport still needs a key to be set
    monkeypatch.setattr(C, "MD_DIR", tmp_path / "md")
    monkeypatch.setattr(C, "FIG_DIR", tmp_path / "figures")
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(C, "REPORT_DIR", tmp_path / "reports")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "sync_2026-11-10.md").write_text(REPORT, encoding="utf-8")
    pdf = make_pdf(tmp_path / "9-9_202610.pdf", [(50, 80, "2.3.2.4 Limit paint stock to 45 kg (99 lb) per cabinet.")])
    conn = D.connect()
    D.replace_document(
        conn,
        pdf,
        text_models=["t1"],
        image_models=["i1"],
        md_dir=C.MD_DIR,
        fig_dir=C.FIG_DIR,
        embedders=fake_embedders(["t1", "i1"]),
        converter=fake_converter({"2026-10": BODY_V1}),
    )
    conn.close()
    # no network, no jobs module and no catalog unless a test opts in
    monkeypatch.setattr(W.hooks, "embedder_factory", lambda k: fake_embedders([k])[k])
    monkeypatch.setattr(C, "DEFAULT_RERANKER", None)  # a pilot config may enable one; tests stay free of API calls
    monkeypatch.setattr(W.hooks, "reranker_factory", lambda k: pytest.fail("reranker must not be called"))
    monkeypatch.setattr(W, "jobs_module", lambda: None)
    return tmp_path


def make_app(*extra: APIRouter) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for r in extra:
        app.include_router(r)
    app.include_router(router)
    return app


@pytest.fixture()
def client(db_env):
    return TestClient(make_app())


# ------------------------------------------------------------------ every page renders
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/documents",
        "/documents?q=9-9&status=unknown&sort=title&dir=desc",
        "/documents/9-9",
        "/documents/9-9?tab=figures",
        "/documents/9-9?unit=2.3.2.4",
        "/documents/9-9?unit=Table%209a",
        "/documents/9-9?unit=9.9.9",
        "/documents/9-9/toc",
        "/documents/9-9/toc?prefix=2.3",
        "/documents/9-9/unit?ref=2.3.2.4",
        "/search",
        "/search?mode=lookup&ds=9-9&ref=pkt+2.3.2.4&children=1&neighbors=1",
        "/search?mode=fulltext&q=%2245+kg%22",
        "/search?mode=hybrid&q=limit+paint",
        "/sync",
        "/sync/jobs",
        "/sync/reports/sync_2026-11-10.md",
        "/citations",
        "/citations/results",
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/vendor/htmx.min.js",
        "/media/figures/9-9/Fig.%202.3.1.png",
    ],
)
def test_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:300]


def test_unknown_sheet_and_report_are_404_pages(client):
    assert client.get("/documents/1-1").status_code == 404
    assert client.get("/sync/reports/nope.md").status_code == 404
    assert client.get("/sync/reports/..%2f..%2fREADME.md").status_code == 404
    assert client.get("/media/figures/9-9/nope.png").status_code == 404


def test_htmx_partials_swap_only_the_target(client):
    full = client.get("/documents").text
    part = client.get("/documents", headers={**HX, "HX-Target": "documents-table"}).text
    assert "<html" in full and "<html" not in part and "9-9" in part
    res = client.get("/search?mode=fulltext&q=kg", headers={**HX, "HX-Target": "results"}).text
    assert "<html" not in res and "45 kg" in res


def test_overview_and_sheet_page_content(client):
    assert "Test Sheet" in client.get("/documents").text
    detail = client.get("/documents/9-9?unit=2.3.2.4").text
    assert "45 kg (99 lb)" in detail
    assert "ASC 9-9" in detail


# ------------------------------------------------------------------ search tabs
def test_lookup_hit_and_not_found(client):
    hit = client.get("/search?mode=lookup&ds=9-9&ref=pkt+2.3.2.4").text
    assert "Limit paint stock to 45 kg (99 lb) per cabinet." in hit
    miss = client.get("/search?mode=lookup&ds=9-9&ref=2.3.2.9").text
    assert "notfound" in miss and "2.3.2.4" in miss  # explicit not-found plus nearest suggestion
    assert "Limit paint stock to 45 kg" not in miss
    nods = client.get("/search?mode=lookup&ref=2.3.2.4").text
    assert "Podaj numer dokumentu" in nods
    routed = client.get("/search?mode=lookup&ref=DS+9-9+pkt+2.3.2.4").text
    assert "45 kg (99 lb)" in routed


def test_fulltext_tab(client):
    html = client.get("/search?mode=fulltext&q=%2245+kg%22").text
    assert "45 kg" in html and "<mark" in html
    assert "2.3.2.4" in html
    none = client.get("/search?mode=fulltext&q=zzzqqq").text
    assert "Brak trafień" in none or "0" in none
    bad = client.get("/search?mode=fulltext&q=%22unbalanced&syntax=fts5")
    assert bad.status_code == 200  # bad FTS5 syntax is reported on the page, never a 500


def test_hybrid_with_mocked_embeddings(client):
    html = client.get("/search?mode=hybrid&q=limit+paint+per+cabinet").text
    assert "Pozycja 1" in plain(html) and "podobieństwo" in plain(html)  # a vector hit is shown
    assert "wektorowe jest niedostępne" not in html


def test_hybrid_degrades_when_embedding_fails(client, monkeypatch):
    def boom(_key):
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    monkeypatch.setattr(W.hooks, "embedder_factory", boom)
    r = client.get("/search?mode=hybrid&q=limit+paint+per+cabinet")
    assert r.status_code == 200
    assert "Wyszukiwanie wektorowe jest niedostępne" in r.text
    assert "OPENROUTER_API_KEY is not set" in r.text
    assert "Limit paint" in r.text  # BM25 results still shown


def test_hybrid_degrades_when_reranker_fails(client, monkeypatch):
    def boom(_key):
        raise RuntimeError("rerank service down")

    monkeypatch.setattr(W.hooks, "reranker_factory", boom)
    r = client.get("/search?mode=hybrid&q=limit+paint+per+cabinet&rerank=r3")
    assert r.status_code == 200
    assert "Reranker nie zadziałał" in r.text and "Pozycja 1" in plain(r.text)


# ------------------------------------------------------------------ verify quote
def test_verify_quote_ok_and_no_match(client):
    ok = client.post(
        "/verify", data={"ds": "9-9", "ref": "2.3.2.4", "quote": "Limit paint stock to 45 kg (99 lb)"}, headers=HX
    )
    assert ok.status_code == 200 and "OK: zgodny" in ok.text and "NO MATCH" not in ok.text
    bad = client.post(
        "/verify", data={"ds": "9-9", "ref": "2.3.2.4", "quote": "Limit paint stock to 45 kg (90 lb)"}, headers=HX
    )
    assert bad.status_code == 200 and "NO MATCH" in bad.text and "OK: zgodny" not in bad.text
    empty = client.post("/verify", data={"ds": "9-9", "ref": "", "quote": ""}, headers=HX)
    assert "Wklej cytat" in empty.text


def test_verify_requires_htmx_header(client):
    assert client.post("/verify", data={"ds": "9-9", "ref": "2.3.2.4", "quote": "x"}).status_code == 400
    assert client.post("/sync/catalog-refresh").status_code == 400
    assert client.post("/citations/run").status_code == 400


# ------------------------------------------------------------------ XSS
XSS = "<script>alert(1)</script>"


@pytest.mark.parametrize("mode", ["lookup", "fulltext", "hybrid"])
def test_query_is_escaped(client, mode):
    key = "ref" if mode == "lookup" else "q"
    r = client.get("/search", params={"mode": mode, key: XSS, "ds": "9-9"})
    assert r.status_code == 200
    assert XSS not in r.text and "&lt;script&gt;" in r.text


def test_verify_quote_is_escaped(client):
    r = client.post("/verify", data={"ds": "9-9", "ref": "2.3.2.4", "quote": XSS}, headers=HX)
    assert XSS not in r.text


def test_unit_and_report_names_are_escaped(client):
    assert XSS not in client.get("/documents/9-9", params={"unit": XSS}).text
    assert XSS not in client.get(f"/sync/reports/{XSS}").text
    assert XSS not in client.get("/documents", params={"q": XSS}).text


# ------------------------------------------------------------------ optional integrations
def test_missing_jobs_and_api_give_empty_states(client):
    for path in ("/", "/sync", "/sync/jobs", "/citations"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "niedostępn" in client.get("/sync/jobs").text
    assert client.get("/sync/jobs/abc").status_code == 200  # unknown job -> small message, not a 500
    for path in ("/sync/catalog-refresh", "/citations/run"):
        r = client.post(path, headers=HX)
        assert r.status_code == 200 and "nie jest dostępny" in r.text


@dataclass
class FakeJob:
    id: str = "j-1"
    kind: str = "sync"
    status: str = "running"
    progress: float | None = 0.5
    message: str = "Osadzanie"
    log_tail: list[str] = field(default_factory=lambda: ["batch 1"])
    params: dict[str, Any] = field(default_factory=dict)
    created_at: str = "2026-11-10T10:00:00"
    started_at: str | None = "2026-11-10T10:00:01"
    finished_at: str | None = None
    result: Any = None
    error: str | None = None


def test_jobs_module_present(db_env, monkeypatch):
    jobs = types.SimpleNamespace(
        list_jobs=lambda limit=50: [FakeJob(), FakeJob("j-2", status="failed", error="Boom", progress=None)],
        get_job=lambda jid: FakeJob(jid),
        submit_job=lambda kind, params: "j-9",
    )
    monkeypatch.setattr(W, "jobs_module", lambda: jobs)
    c = TestClient(make_app())
    sync = c.get("/sync").text
    assert "Osadzanie" in sync and "Boom" in sync
    card = c.get("/sync/jobs/j-1?poll=1", headers=HX).text
    assert "Osadzanie" in card and "hx-trigger" in card  # still active -> keeps polling
    assert "Osadzanie" in c.post("/sync/catalog-refresh", headers=HX).text

    def raising(kind, params):
        raise RuntimeError("queue full")

    jobs.submit_job = raising
    assert "queue full" in c.post("/citations/run", data={"repo": ""}, headers=HX).text


def test_api_endpoints_present(db_env):
    api = APIRouter(prefix="/api")
    api.get("/collection")(
        lambda: [
            {"doc_id": "9-9", "catalog_status": "outdated", "latest_edition": "April 2027"},
            {"doc_id": "4-4", "title": "Only remote"},
        ]
    )
    api.get("/stats")(lambda: {"cost_usd": 1.5, "last_sync": "2026-11-10T09:00:00"})
    api.get("/citations/latest")(
        lambda: {
            "generated_at": "2026-11-10T09:00:00",
            "results": [
                {
                    "file": "a.md",
                    "line": 3,
                    "citation": "ASC 9-9 pkt 2.3.2.9",
                    "doc_id": "9-9",
                    "unit_id": "2.3.2.9",
                    "status": "UNKNOWN UNIT",
                    "suggestion": "nearest: 2.3.2.4",
                },
                {
                    "file": "b.md",
                    "line": 5,
                    "citation": "ASC 9-9 pkt 2.3.2.4",
                    "doc_id": "9-9",
                    "unit_id": "2.3.2.4",
                    "status": "OK",
                    "suggestion": "",
                },
            ],
        }
    )
    c = TestClient(make_app(api))
    sheets = c.get("/documents").text
    assert "Do odświeżenia" in sheets and "April 2027" in sheets and "4-4" in sheets
    assert "1,50 USD" in plain(c.get("/").text)
    cit = c.get("/citations").text
    assert "a.md:3" in cit and "NIEZNANY PUNKT" in cit and "najbliższe: 2.3.2.4" in cit
    only_ok = c.get("/citations/results?status=ok").text
    assert "b.md:5" in only_ok and "a.md:3" not in only_ok


def test_broken_api_payload_does_not_500(db_env):
    api = APIRouter(prefix="/api")
    api.get("/collection")(lambda: "garbage")
    api.get("/stats")(lambda: [1, 2])
    api.get("/citations/latest")(lambda: {"results": [{"status": None}, "x", 5]})
    c = TestClient(make_app(api))
    for path in ("/", "/documents", "/citations"):
        assert c.get(path).status_code == 200, path


def test_client_snippets_are_valid_json_and_reference_the_env_var():
    import json

    from vedun.web.mcp_setup import client_snippets

    snips = {s.key: s for s in client_snippets("http://127.0.0.1:8765/mcp", "kb", "KB_KEY")}
    cursor = json.loads(snips["cursor"].text)["mcpServers"]["kb"]
    assert cursor == {"url": "http://127.0.0.1:8765/mcp", "headers": {"X-API-Key": "${env:KB_KEY}"}}
    vscode = json.loads(snips["vscode"].text)
    assert vscode["servers"]["kb"]["type"] == "http" and vscode["inputs"][0]["password"] is True
    entry = json.loads(snips["claude-code"].text.split("'")[1])
    assert entry == {"type": "http", "url": "http://127.0.0.1:8765/mcp", "headers": {"X-API-Key": "${KB_KEY}"}}
    desktop = json.loads(snips["claude-desktop"].text)["mcpServers"]["kb"]
    assert "X-API-Key:${MCP_KEY}" in desktop["args"] and " " not in desktop["args"][-1]


# ------------------------------------------------------------------ cancel button
class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__("English detail that must not reach the page")
        self.reason = reason


def _jobs_with(job: FakeJob, cancel=None, cancellable=("sync", "check_citations")):
    return types.SimpleNamespace(
        list_jobs=lambda limit=50: [job],
        get_job=lambda jid: job,
        submit_job=lambda kind, params: "j-9",
        cancel_job=cancel or (lambda jid: None),
        CANCELLABLE_WHILE_RUNNING=frozenset(cancellable),
    )


def test_job_card_offers_anuluj_only_where_a_cancel_can_work(db_env, monkeypatch):
    def card(job: FakeJob, **kw) -> str:
        monkeypatch.setattr(W, "jobs_module", lambda: _jobs_with(job, **kw))
        return TestClient(make_app()).get(f"/sync/jobs/{job.id}?poll=1", headers=HX).text

    running = card(FakeJob())
    assert "Anuluj" in running and 'hx-post="/sync/jobs/j-1/cancel?page=sync"' in running and "hx-confirm" in running
    assert 'hx-target="#job-j-1"' in running and 'id="cancel-msg"' in running
    queued = card(FakeJob(status="queued", kind="catalog_refresh", progress=None))
    assert "Anuluj" in queued  # any queued job can be dropped
    no_safe_point = card(FakeJob(kind="catalog_refresh"))
    assert "/cancel" not in no_safe_point  # running refresh: no safe point, so no button to promise anything
    done = card(FakeJob(status="done", finished_at="2026-11-10T10:05:00", progress=1.0))
    assert "/cancel" not in done and "Zakończone" in done
    gone = card(FakeJob(status="cancelled", finished_at="2026-11-10T10:05:00", message="Anulowano: dodano 1"))
    assert "Anulowane" in gone and "Anulowano: dodano 1" in gone and "/cancel" not in gone
    old_module = types.SimpleNamespace(get_job=lambda jid: FakeJob(), list_jobs=lambda limit=50: [])
    monkeypatch.setattr(W, "jobs_module", lambda: old_module)  # an older jobs module without the feature
    assert "/cancel" not in TestClient(make_app()).get("/sync/jobs/j-1", headers=HX).text


def test_job_card_shows_a_pending_cancel(db_env, monkeypatch):
    @dataclass
    class Asked(FakeJob):
        cancel_requested: bool = True

    monkeypatch.setattr(W, "jobs_module", lambda: _jobs_with(Asked()))
    html = TestClient(make_app()).get("/sync/jobs/j-1?poll=1", headers=HX).text
    assert "Anulowanie zgłoszone" in html and "Anulowanie…" in html and "disabled" in html
    assert "hx-trigger" in html  # still polling until the job really ends


def test_cancel_route_requires_htmx_and_maps_refusals_to_polish(db_env, monkeypatch):
    seen: list[str] = []

    def cancel(jid):
        seen.append(jid)
        if jid == "done-1":
            raise _Refused("finished")
        if jid == "refresh-1":
            raise _Refused("no_safe_point")
        if jid == "nope":
            raise KeyError(jid)

    monkeypatch.setattr(W, "jobs_module", lambda: _jobs_with(FakeJob(), cancel=cancel))
    c = TestClient(make_app())
    assert c.post("/sync/jobs/j-1/cancel").status_code == 400 and seen == []  # no HX-Request: nothing is called
    ok = c.post("/sync/jobs/j-1/cancel?page=sync", headers=HX)
    assert ok.status_code == 200 and seen == ["j-1"] and 'id="job-j-1"' in ok.text and "hx-trigger" in ok.text
    for jid, text in (
        ("done-1", "już się zakończyło"),
        ("refresh-1", "nie można przerwać"),
        ("nope", "Nie znaleziono takiego zadania"),
    ):
        r = c.post(f"/sync/jobs/{jid}/cancel", headers=HX)
        assert r.status_code == 200 and text in r.text and "English detail" not in r.text, jid
        assert r.headers["HX-Retarget"] == "#cancel-msg" and r.headers["HX-Reswap"] == "innerHTML"
    monkeypatch.setattr(W, "jobs_module", lambda: types.SimpleNamespace(get_job=lambda jid: None))
    assert "nie jest dostępny" in c.post("/sync/jobs/j-1/cancel", headers=HX).text


def test_demo_app_serves_every_page_including_connect(tmp_path, monkeypatch):
    """The demo fakes only ``vedun.server.jobs``: /connect still imports the real mcp_app, mcp_tools and settings."""
    import sys

    import vedun.server
    import vedun.server.jobs as real_jobs
    from vedun.web import dev_app

    monkeypatch.setitem(sys.modules, "vedun.server.jobs", real_jobs)  # restored after the test
    monkeypatch.setattr(vedun.server, "jobs", real_jobs)
    monkeypatch.setattr(C, "REPORT_DIR", C.REPORT_DIR)
    monkeypatch.setenv("VEDUN_WEB_DEMO_DIR", str(tmp_path))
    c = TestClient(dev_app.create_app(demo=True))
    connect = c.get("/connect")
    assert connect.status_code == 200 and "verify_quote" in connect.text
    assert c.get("/sync").status_code == 200 and c.get("/citations").status_code == 200
