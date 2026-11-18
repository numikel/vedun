"""The dashboard and the public texts are corpus-neutral: brand, subtitle and catalog wording come from the profile."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_web_pages import db_env, make_app, plain  # noqa: F401 - db_env registers the fixture
from vedun import config as C
from vedun.web import STATIC_DIR

#: words a private instance must never leak into the generic UI (regex, case-insensitive), set by that instance's
#: maintainer; the public repository names no publisher, so without it the publisher checks are skipped
_FORBIDDEN = os.environ.get("NEUTRAL_CHECK_WORDS", "")
PUBLISHER = re.compile(_FORBIDDEN, re.IGNORECASE) if _FORBIDDEN else None
needs_words = pytest.mark.skipif(PUBLISHER is None, reason="set NEUTRAL_CHECK_WORDS to check for a private corpus")
PAGES = [
    "/",
    "/documents",
    "/documents/9-9",
    "/documents/9-9?unit=2.3.2.4",
    "/documents/9-9?unit=9.9.9",
    "/documents/1-1",
    "/search",
    "/search?mode=lookup",
    "/search?mode=lookup&ds=1-1&ref=2.3.1",
    "/search?mode=fulltext",
    "/search?mode=hybrid",
    "/sync",
    "/sync/reports/sync_2026-11-10.md",
    "/citations",
    "/connect",
]
SOURCE_PLUGIN = (
    "class _Source:\n"
    "    name = 'example.org'\n"
    "    def fetch(self): ...\n"
    "    def download(self, entry, dest): ...\n"
    "SOURCE = _Source()\n"
)


@pytest.fixture(params=["en", "pl"])
def lang_client(request, db_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(C, "UI_LANGUAGE", request.param)
    monkeypatch.setattr(C, "DEFAULT_PDF_DIR", db_env / "pdf")  # a checkout path may contain any word
    return TestClient(make_app()), request.param


def test_brand_is_vedun_and_subtitle_is_the_corpus_name(lang_client):
    client, _ = lang_client
    html = client.get("/").text
    assert "<strong>vedun</strong><small>ACME Safety Code</small>" in html
    assert "· vedun</title>" in html


@needs_words
@pytest.mark.parametrize("path", PAGES)
def test_no_publisher_wording_in_rendered_pages(lang_client, path):
    client, _ = lang_client
    html = client.get(path).text
    assert not PUBLISHER.search(html), PUBLISHER.search(html)


@needs_words
def test_no_publisher_wording_in_static_files():
    for name in ("js/app.js", "css/app.css"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert not PUBLISHER.search(text), (name, PUBLISHER.search(text))


@needs_words
def test_no_publisher_wording_in_code_docs_skills_and_tool_descriptions():
    """Examples in docstrings, MCP tool descriptions, docs and skills are what agents and readers see."""
    root = Path(__file__).resolve().parent.parent
    files = [
        *(root / "vedun").rglob("*.py"),
        *(root / "vedun").rglob("*.html"),
        *(root / "docs").rglob("*.md"),
        *(root / "skills").rglob("*.md"),
        root / "AGENTS.md",
        root / "README.md",
    ]
    hits = [
        (f.relative_to(root).as_posix(), m.group(0)) for f in files for m in PUBLISHER.finditer(f.read_text("utf-8"))
    ]
    assert not hits, hits[:10]


def test_catalog_wording_names_the_configured_source(db_env, monkeypatch, tmp_path):  # noqa: F811
    plugin = tmp_path / "named_source.py"
    plugin.write_text(SOURCE_PLUGIN, encoding="utf-8")
    monkeypatch.setattr(C, "SOURCE_PLUGIN", plugin)
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    client = TestClient(make_app())
    sync = plain(client.get("/sync").text)
    assert "Catalog of example.org" in sync
    assert "Fetch newer editions from example.org" in sync
    assert "Refresh the catalog of example.org" in sync
    assert "Status against example.org" in plain(client.get("/documents").text)
    assert "Freshness against example.org" in plain(client.get("/").text)
    monkeypatch.setattr(C, "UI_LANGUAGE", "pl")
    assert "Katalog example.org" in plain(client.get("/sync").text)
    assert "Stan względem example.org" in plain(client.get("/documents").text)


def test_catalog_wording_without_a_source_is_neutral(db_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(C, "SOURCE_PLUGIN", None)
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    client = TestClient(make_app())
    html = client.get("/sync").text
    assert "Catalog of the source" in plain(html)
    # nothing to fetch from: no fetch option, no refresh button, a pointer to the plugin guide instead
    assert 'name="fetch_from_source"' not in html and "/sync/catalog-refresh" not in html
    assert "#writing-a-source-plugin" in html and "No document source is configured" in plain(html)
    assert "Status against the source" in plain(client.get("/documents").text)
    monkeypatch.setattr(C, "UI_LANGUAGE", "pl")
    assert "Katalog źródła" in plain(client.get("/sync").text)


def test_sync_form_posts_fetch_from_source(db_env, monkeypatch, tmp_path):  # noqa: F811
    plugin = tmp_path / "named_source.py"
    plugin.write_text(SOURCE_PLUGIN, encoding="utf-8")
    monkeypatch.setattr(C, "SOURCE_PLUGIN", plugin)
    monkeypatch.setattr(C, "UI_LANGUAGE", "en")
    html = TestClient(make_app()).get("/sync").text
    assert 'name="fetch_from_source"' in html and 'data-field-fetch="fetch_from_source"' in html
    assert "fetch_from_fm" not in html
    assert "fetch_from_fm" not in (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")


def test_old_sheet_urls_are_gone(db_env):  # noqa: F811
    client = TestClient(make_app())
    assert client.get("/documents").status_code == 200
    assert client.get("/sheets").status_code == 404
    assert client.get("/sheets/9-9").status_code == 404


def test_report_headings_old_and_new_parse():
    from vedun.web import reports as R

    new = "# vedun sync report 2026-11-10 23:14\n\nAdded: 9-9; replaced: -; removed (PDF gone): -; unchanged: 0.\n\n"
    new += "## Document 9-9: new document, edition 2026-10 (10 units, 12 chunks)\n"
    old = new.replace("# vedun sync report", "# Legacy brand sync report").replace(
        "## Document 9-9: new document", "## DS 9-9: new sheet"
    )
    for text in (new, old):
        rep = R.parse_report(text, "sync_x.md")
        assert rep.generated == "2026-11-10 23:14"
        assert [(s.ds, s.kind, s.new_edition) for s in rep.sections] == [("9-9", "new", "2026-10")]
