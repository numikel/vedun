"""The plugin example in examples/ keeps working: it loads as a source, builds a valid catalog and downloads."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from vedun import catalog as K
from vedun import config as C

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "source_plugin.py"
LISTING = [
    {"id": "4-12", "title": "Ladder Inspection", "published": "October 2026", "pdf": "https://example.org/4-12.pdf"},
    {"id": "11-7", "title": "Tool Storage", "published": "April 2025", "pdf": "https://example.org/11-7.pdf"},
    {"id": "not an id", "title": "Dropped", "published": "May 2024", "pdf": "https://example.org/x.pdf"},
]


def _example_module():
    spec = importlib.util.spec_from_file_location("vedun_example_source", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_example_builds_a_valid_catalog_without_network(monkeypatch):
    mod = _example_module()
    monkeypatch.setattr(mod, "http_json", lambda url: LISTING)
    cat = K.validated(mod.SOURCE.fetch())
    assert set(cat.entries) == {"4-12", "11-7"}  # an id outside doc_id_pattern is dropped, with a warning
    assert cat.entries["4-12"].edition == "2026-10" and cat.entries["11-7"].edition == "2025-04"
    assert any("not an id" in w for w in cat.warnings)


def test_example_downloads_through_the_entry_item_id(monkeypatch, tmp_path):
    mod = _example_module()
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(mod, "http_download", lambda url, dest: calls.append((url, dest)) or 1234)
    entry = K.CatalogEntry("4-12", "Ladder Inspection", "https://example.org/4-12.pdf", "October 2026", "2026-10")
    assert mod.SOURCE.download(entry, tmp_path / "x.part") == 1234
    assert calls == [("https://example.org/4-12.pdf", tmp_path / "x.part")]


def test_profile_can_point_at_the_example(monkeypatch):
    monkeypatch.setattr(C, "SOURCE_PLUGIN", EXAMPLE)
    assert K.source_name() == "example.org"
