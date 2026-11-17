"""Bilingual dashboard: language choice (profile / cookie / query), English and Polish pages, catalogue integrity."""

from __future__ import annotations

import json
import re

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from tests import web_i18n_scan as scan
from tests.test_web_pages import HX, FakeJob, _jobs_with, db_env, make_app, plain  # noqa: F401 - db_env registers the fixture
from vedun import config as C
from vedun.web import catalog_pl, i18n
from vedun.web import data as W

DIACRITICS = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
PAGES = [
    "/",
    "/documents",
    "/documents/9-9",
    "/documents/9-9?tab=figures",
    "/documents/9-9?unit=2.3.2.4",
    "/documents/9-9?unit=9.9.9",
    "/search",
    "/search?mode=lookup&ds=9-9&ref=pkt+2.3.2.4&children=1&neighbors=1",
    "/search?mode=lookup&ds=9-9&ref=2.3.2.9",
    "/search?mode=lookup&ref=2.3.2.4",
    "/search?mode=fulltext",
    "/search?mode=fulltext&q=%2245+kg%22",
    "/search?mode=fulltext&q=zzzqqq",
    "/search?mode=hybrid",
    "/search?mode=hybrid&q=limit+paint+per+cabinet",
    "/sync",
    "/sync/reports/sync_2026-11-10.md",
    "/citations",
    "/connect",
    "/documents/1-1",
]


@pytest.fixture()
def web_env(request):
    """The tiny database and offline hooks of ``test_web_pages`` (its ``db_env`` fixture)."""
    return request.getfixturevalue("db_env")


@pytest.fixture()
def english(web_env, monkeypatch):
    """The profile has no ``ui_language`` (default English)."""
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    return TestClient(make_app())


@pytest.fixture()
def polish(web_env, monkeypatch):
    monkeypatch.setattr(C, "UI_LANGUAGE", "pl")
    return TestClient(make_app())


def page_markup(html: str) -> str:
    """The page without the JSON block of JavaScript strings (it carries both languages by design)."""
    return re.sub(r'<script type="application/json" id="i18n-strings">.*?</script>', "", html, flags=re.S)


def html_lang(html: str) -> str:
    m = re.search(r"<html lang=\"([^\"]*)\"", html)
    assert m, "no lang attribute on <html>"
    return m.group(1)


# ------------------------------------------------------------------ catalogue integrity
def test_every_source_text_has_a_polish_entry():
    texts, plurals = scan.template_texts()
    used = texts | scan.python_texts() | scan.js_texts()
    assert not sorted(used - catalog_pl.PL.keys()), "texts without a Polish entry"
    assert not sorted(plurals - catalog_pl.PL_PLURAL.keys()), "plural keys without Polish forms"


def test_catalogue_has_no_stale_entries():
    texts, plurals = scan.template_texts()
    used = texts | scan.python_texts() | scan.js_texts()
    assert not sorted(catalog_pl.PL.keys() - used), "entries no source uses any more"
    assert not sorted(catalog_pl.PL_PLURAL.keys() - plurals), "plural entries no source uses any more"


def test_js_string_list_matches_app_js():
    assert set(catalog_pl.JS_STRINGS) == scan.js_texts()


def test_placeholders_survive_translation():
    field = re.compile(r"{(\w+)}")
    for src, pl in catalog_pl.PL.items():
        assert set(field.findall(src)) == set(field.findall(pl)), src


def test_no_polish_text_outside_the_catalogue():
    for path in [*scan.TEMPLATES.rglob("*.html"), scan.WEB / "static" / "js" / "app.js"]:
        assert not DIACRITICS.search(path.read_text(encoding="utf-8")), path.name


# ------------------------------------------------------------------ language choice
def test_english_is_the_default_without_ui_language(english):
    html = english.get("/").text
    assert html_lang(html) == "en"
    text = plain(html)
    assert "Overview" in text and "Quick search" in text and "Documents" in text
    assert "Przegląd" not in html and "Szybkie wyszukiwanie" not in html


def test_polish_when_the_profile_says_pl(polish):
    html = polish.get("/").text
    assert html_lang(html) == "pl"
    assert "Przegląd" in html and "Szybkie wyszukiwanie" in html and "Quick search" not in html


def test_the_test_profile_itself_sets_polish(web_env):
    assert C.UI_LANGUAGE == "pl"  # tests/data/test_profile.json carries ui_language: pl
    assert html_lang(TestClient(make_app()).get("/").text) == "pl"


@pytest.mark.parametrize(("profile", "choice", "expected"), [("en", "pl", "pl"), ("pl", "en", "en")])
def test_query_overrides_the_profile_and_is_remembered(web_env, monkeypatch, profile, choice, expected):
    monkeypatch.setattr(C, "UI_LANGUAGE", profile)
    c = TestClient(make_app())
    first = c.get(f"/documents?lang={choice}")
    assert html_lang(first.text) == expected
    assert f"{i18n.COOKIE_NAME}={choice}" in first.headers["set-cookie"] and "Cookie" in first.headers["vary"]
    again = c.get("/search")  # no query: the cookie keeps the choice
    assert html_lang(again.text) == expected
    assert "set-cookie" not in again.headers  # nothing is written for ordinary page views


def test_query_beats_the_cookie(english):
    english.cookies.set(i18n.COOKIE_NAME, "pl")
    assert html_lang(english.get("/").text) == "pl"
    assert html_lang(english.get("/?lang=en").text) == "en"
    assert html_lang(english.get("/").text) == "en"  # the new choice replaced the cookie


def test_unknown_values_fall_back_to_the_profile(english):
    assert html_lang(english.get("/?lang=de").text) == "en"  # unknown query
    english.cookies.set(i18n.COOKIE_NAME, "de")
    assert html_lang(english.get("/").text) == "en"  # unknown cookie
    english.cookies.set(i18n.COOKIE_NAME, "pl", domain="")  # a valid cookie is kept when only the query is unknown
    assert html_lang(english.get("/?lang=de").text) == "pl"
    assert "set-cookie" not in english.get("/?lang=de").headers


def test_language_codes_are_exact(polish):
    assert html_lang(polish.get("/?lang=EN").text) == "pl"
    assert html_lang(polish.get("/?lang=").text) == "pl"


def test_switch_links_keep_the_rest_of_the_url(english):
    html = english.get("/documents?q=9-9&status=unknown").text
    links = re.findall(r'<a class="lang-opt[^"]*" href="([^"]+)" data-lang="(\w+)"', html)
    assert dict((code, href) for href, code in links) == {
        "en": "/documents?q=9-9&amp;status=unknown&amp;lang=en",
        "pl": "/documents?q=9-9&amp;status=unknown&amp;lang=pl",
    }
    assert 'aria-current="true"' in html.split('data-lang="en"')[1].split("</a>")[0]  # the active language is marked


def test_language_does_not_leak_between_requests(polish):
    assert html_lang(polish.get("/?lang=en").text) == "en"
    assert i18n.current() == C.UI_LANGUAGE  # the context variable is per request
    assert html_lang(TestClient(make_app()).get("/").text) == "pl"  # fresh client, no cookie


# ------------------------------------------------------------------ whole pages in both languages
def _catalogue_values(*, polish: bool) -> list[str]:
    """Strings that identify one language in rendered HTML (long enough not to occur in data by accident)."""
    if polish:  # "pkt" examples also appear in the MCP tool descriptions, which come from the server module
        return [pl for src, pl in catalog_pl.PL.items() if pl != src and len(pl) > 14 and " pkt " not in pl]
    return [src for src, pl in catalog_pl.PL.items() if pl != src and len(src) > 22 and "{" not in src]


@pytest.mark.parametrize("path", PAGES)
def test_english_pages_contain_no_polish_ui_text(english, path):
    r = english.get(path)
    assert r.status_code in (200, 404), path
    leaked = [v for v in _catalogue_values(polish=True) if v in page_markup(r.text)]
    assert not leaked, f"{path}: Polish text on an English page: {leaked[:3]}"
    assert html_lang(r.text) == "en"


@pytest.mark.parametrize("path", PAGES)
def test_polish_pages_contain_no_english_ui_text(polish, path):
    r = polish.get(path)
    assert r.status_code in (200, 404), path
    leaked = [v for v in _catalogue_values(polish=False) if v in page_markup(r.text)]
    assert not leaked, f"{path}: English text on a Polish page: {leaked[:3]}"
    assert html_lang(r.text) == "pl"


def test_english_search_flows(english):
    assert "Enter a document number" in english.get("/search?mode=lookup&ref=2.3.2.4").text
    miss = english.get("/search?mode=lookup&ds=9-9&ref=2.3.2.9").text
    assert "Not found" in miss and "Nearest existing numbers" in miss
    assert "No hits" in english.get("/search?mode=fulltext&q=zzzqqq").text
    hybrid = plain(english.get("/search?mode=hybrid&q=limit+paint+per+cabinet").text)
    assert "Position 1" in hybrid and "similarity" in hybrid and "candidate" in hybrid
    assert "Exact match by number" in english.get("/search?mode=hybrid&q=DS+9-9+pkt+2.3.2.4").text
    assert "Copy PL" in english.get("/documents/9-9?unit=2.3.2.4").text


def test_english_verify_and_error_messages(english):
    ok = english.post(
        "/verify", data={"ds": "9-9", "ref": "2.3.2.4", "quote": "Limit paint stock to 45 kg (99 lb)"}, headers=HX
    )
    assert "OK: matches the text and the PDF" in ok.text
    bad = english.post(
        "/verify", data={"ds": "9-9", "ref": "2.3.2.4", "quote": "Limit paint stock to 45 kg (90 lb)"}, headers=HX
    )
    assert "NO MATCH" in bad.text and "Do not use this quote" in bad.text
    assert "Paste a quote to verify." in english.post("/verify", data={}, headers=HX).text
    assert english.post("/verify", data={}).json()["detail"] == "An HTMX request is required."
    missing = english.get("/documents/1-1")
    assert (
        missing.status_code == 404 and "No such document" in missing.text and "ASC 1-1 is not indexed" in missing.text
    )
    assert "Report not found" in english.get("/sync/reports/nope.md").text
    assert english.get("/media/figures/9-9/nope.png").json()["detail"] == "No image for this figure."


def test_english_jobs_pages(web_env, monkeypatch):
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    c = TestClient(make_app())
    assert "The jobs module is not available in this instance." in c.get("/sync").text
    for path in ("/sync/catalog-refresh", "/citations/run"):
        assert "The jobs module is not available" in c.post(path, headers=HX).text
    job = FakeJob(kind="catalog_refresh", status="queued", progress=None, message="")
    monkeypatch.setattr(W, "jobs_module", lambda: _jobs_with(job))
    card = c.get("/sync/jobs/j-1?poll=1", headers=HX).text
    assert "Catalog refresh" in card and "Queued" in card and "Waiting in the queue" in card and ">Cancel<" in card
    monkeypatch.setattr(W, "jobs_module", lambda: _jobs_with(FakeJob()))
    assert "Document sync" in c.get("/sync/jobs/j-1?poll=1", headers=HX).text


def test_english_catalog_and_citation_pages(web_env, monkeypatch):
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    api = APIRouter(prefix="/api")
    api.get("/collection")(lambda: [{"doc_id": "9-9", "catalog_status": "outdated", "latest_edition": "April 2027"}])
    api.get("/stats")(lambda: {"cost_usd": 1.5, "last_sync": "2026-11-10T09:00:00"})
    api.get("/citations/latest")(
        lambda: {
            "results": [
                {
                    "file": "a.md",
                    "line": 3,
                    "citation": "ASC 9-9 pkt 2.3.2.9",
                    "status": "UNKNOWN UNIT",
                    "suggestion": "renumbered to 2.3.2.4; nearest: 2.3.2.5",
                },
            ]
        }
    )
    c = TestClient(make_app(api))
    sheets = plain(c.get("/documents").text)
    assert "Needs refresh" in sheets and "April 2027" in sheets and "1 of 1 document" in sheets
    home = plain(c.get("/").text)
    assert "1.50 USD" in home and re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", home)
    cit = plain(c.get("/citations").text)
    assert "UNKNOWN CLAUSE" in cit and "renumbered to 2.3.2.4; nearest: 2.3.2.5" in cit
    pl = TestClient(make_app(api))
    cit_pl = plain(pl.get("/citations?lang=pl").text)
    assert "NIEZNANY PUNKT" in cit_pl and "przenumerowano na 2.3.2.4; najbliższe: 2.3.2.5" in cit_pl
    home_pl = plain(pl.get("/?lang=pl").text)
    assert "1,50 USD" in home_pl and re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", home_pl)


def test_mcp_page_and_snippets_follow_the_language(english):
    en = plain(english.get("/connect").text)
    assert "Client configuration" in en and "Claude Code substitutes" in en and "outside a claude session" in en
    pl = plain(english.get("/connect?lang=pl").text)
    assert "Konfiguracja klientów" in pl and "Claude Code sam podstawia" in pl and "poza sesją claude" in pl


def test_javascript_strings_are_embedded_for_polish_only(english):
    en_blob = re.search(r'id="i18n-strings">(.*?)</script>', english.get("/").text, re.S)
    pl_blob = re.search(r'id="i18n-strings">(.*?)</script>', english.get("/?lang=pl").text, re.S)
    assert en_blob and en_blob.group(1) == "{}"
    assert pl_blob
    pl_strings = json.loads(pl_blob.group(1))
    assert set(pl_strings) == set(catalog_pl.JS_STRINGS)
    assert pl_strings["Dark theme"] == "Motyw ciemny"
    assert "Skopiowano do schowka" in pl_strings.values()


# ------------------------------------------------------------------ helpers
def test_t_escapes_arguments_and_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(C, "UI_LANGUAGE", "pl")
    safe = i18n.t("{doc} is not indexed in this database.", doc="<script>")
    assert "&lt;script&gt;" in safe and "<script>" not in safe and safe.startswith("&lt;script&gt; nie jest")
    assert i18n.t("A text nobody translated") == "A text nobody translated"
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    assert i18n.t("{doc} is not indexed in this database.", doc="9-9") == "9-9 is not indexed in this database."


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "dokument"),
        (2, "dokumenty"),
        (4, "dokumenty"),
        (5, "dokumentów"),
        (12, "dokumentów"),
        (22, "dokumenty"),
        (0, "dokumentów"),
    ],
)
def test_polish_plural_forms(monkeypatch, n, expected):
    monkeypatch.setattr(C, "UI_LANGUAGE", "pl")
    assert i18n.plural(n, "document", "documents") == expected


def test_english_plural_and_formats(monkeypatch):
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    assert [i18n.plural(n, "document", "documents") for n in (0, 1, 2)] == ["documents", "document", "documents"]
    assert W.fmt_int(1234567) == "1,234,567" and W.fmt_usd(1.5) == "1.50 USD" and W.fmt_bytes(1536) == "1.5 kB"
    monkeypatch.setattr(C, "UI_LANGUAGE", "pl")
    assert W.fmt_int(1234567) == "1 234 567" and W.fmt_usd(1.5) == "1,50 USD" and W.fmt_bytes(1536) == "1,5 kB"


# ------------------------------------------------------------------ job messages (stored in English)
@pytest.mark.parametrize(
    ("message", "polish"),
    [
        ("added 3, replaced 0, removed 0, unchanged 2", "dodano 3, zastąpiono 0, usunięto 0, bez zmian 2"),
        (
            "Dry run: added 1, replaced 0, removed 0, unchanged 0",
            "Próba (dry run): dodano 1, zastąpiono 0, usunięto 0, bez zmian 0",
        ),
        (
            "Cancelled: added 1, replaced 0, removed 0, skipped 1 (9-8), failed 1: 9-7",
            "Anulowano: dodano 1, zastąpiono 0, usunięto 0, pominięto 1 (9-8), nieudane 1: 9-7",
        ),
        ("Done", "Zakończono"),
        ("Interrupted by a service restart", "Przerwane przez restart usługi"),
        ("Catalog: 222 documents ({'current': 222})", "Katalog: 222 dokumentów ({'current': 222})"),
        ("Checked 25/40 files", "Sprawdzono 25/40 plików"),
        ("12 citations in 3 files: {'OK': 12}", "12 cytowań w 3 plikach: {'OK': 12}"),
        ("[9-9] converting 9-9_202601.pdf", "[9-9] converting 9-9_202601.pdf"),  # progress lines stay as logged
    ],
)
def test_job_messages_are_english_and_translated_in_polish(monkeypatch, message, polish):
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    assert W.localize_job_message(message) == message
    monkeypatch.setattr(C, "UI_LANGUAGE", "pl")
    assert W.localize_job_message(message) == polish


# ------------------------------------------------------------------ search examples come from the collection
EXAMPLE_PAGES = ["/", "/search?mode=lookup", "/search?mode=fulltext", "/search?mode=hybrid", "/citations"]


@pytest.mark.parametrize("path", EXAMPLE_PAGES)
def test_examples_come_from_the_indexed_collection(english, path):
    ex = W.search_examples()
    assert ex is not None and ex.refs and ex.refs[0][0] == "clause"
    html = english.get(path).text
    assert "3-26" not in html and "storage height" not in html and "2-81" not in html
    assert ex.ds in html or (ex.phrases and ex.phrases[0] in html), path


def test_lookup_example_links_open_a_real_unit(english):
    ex = W.search_examples()
    kind, uid = ex.refs[0]
    hit = english.get(f"/search?mode=lookup&ds={ex.ds}&ref={uid}").text
    assert "not found" not in hit.lower() and ex.locator(kind, uid) in hit


@pytest.mark.parametrize("path", EXAMPLE_PAGES)
def test_an_empty_collection_renders_without_examples(english, monkeypatch, path):
    from vedun.web import routes

    monkeypatch.setitem(routes.templates.env.globals, "examples", lambda: None)
    r = english.get(path)
    assert r.status_code == 200 and "3-26" not in r.text
