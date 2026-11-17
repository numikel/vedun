"""Document catalog: statuses, cache, source-plugin loading and validation, download/retire rules. No network."""

from __future__ import annotations

import pytest

from tests.helpers_server import FakeSource, fake_catalog
from vedun import catalog as K
from vedun import config as C


@pytest.mark.parametrize(
    ("label", "ed"), [("October 2026", "2026-10"), ("January 2001", "2001-01"), ("—", None), (None, None)]
)
def test_edition_from_published(label, ed):
    assert K.edition_from_published(label) == ed


def test_status_current_outdated_missing_withdrawn(tmp_path):
    cat = fake_catalog("21-4")
    (tmp_path / "21-4_202510.pdf").write_bytes(b"%PDF-1")  # older than the source -> outdated
    (tmp_path / "22-6_202604.pdf").write_bytes(b"%PDF-1")  # as new -> current
    (tmp_path / "1-1_202001.pdf").write_bytes(b"%PDF-1")  # not in the catalog -> withdrawn
    by = {s.doc_id: s for s in K.catalog_status(None, cat, tmp_path)}
    assert by["21-4"].status == "outdated" and by["21-4"].local_edition == "2025-10"
    assert by["22-6"].status == "current"
    assert by["1-1"].status == "withdrawn"
    assert by["25-3"].status == "missing_locally"
    assert K.counts_by_status(list(by.values())) == {"outdated": 1, "current": 1, "withdrawn": 1, "missing_locally": 4}
    assert [s.status for s in K.catalog_status(None, None, tmp_path)] == ["unknown"] * 3  # no snapshot yet


def test_cache_roundtrip_and_empty_catalog_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    assert K.cached_catalog() is None
    cat = K.fetch_catalog(source=FakeSource())
    again = K.cached_catalog()
    assert again is not None and again.fetched_at == cat.fetched_at and set(again.entries) == set(cat.entries)
    never = FakeSource(catalog=lambda: pytest.fail("must use the cache"))
    assert K.fetch_catalog(max_age_s=3600, source=never).fetched_at == cat.fetched_at
    with pytest.raises(RuntimeError, match="empty catalog"):
        K.fetch_catalog(force=True, source=FakeSource(catalog=K.Catalog()))
    assert K.cached_catalog().fetched_at == cat.fetched_at  # failed refresh left the snapshot alone


def test_entries_a_source_must_not_hand_over_are_dropped(tmp_path, monkeypatch):
    """The id and edition become a file name: a hostile or buggy source cannot point outside the PDF folder."""
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    cat = fake_catalog("21-4")
    bad = {
        "../x": K.CatalogEntry("../x", "t", "g", None, None),
        "1-1": K.CatalogEntry("1-1", "t", "g", "May 2020", "2020-5"),
        "1-2": K.CatalogEntry("1-3", "t", "g", None, None),
    }
    cat.entries.update(bad)
    got = K.fetch_catalog(source=FakeSource(catalog=cat))
    assert set(got.entries) == set(fake_catalog("21-4").entries)
    assert len(got.warnings) == 3 and all("dropped" in w for w in got.warnings)
    with pytest.raises(ValueError, match="refusing file name"):
        K.download_pdf(K.CatalogEntry("1-1/../../x", "t", "g", None, "2026-01"), tmp_path, FakeSource())


def test_without_a_plugin_the_catalog_says_so(monkeypatch):
    monkeypatch.setattr(C, "SOURCE_PLUGIN", None)
    with pytest.raises(K.NoSourceError, match="source_plugin"):
        K.fetch_catalog(force=True)


def test_plugin_is_loaded_from_the_profile_path(tmp_path, monkeypatch):
    plugin = tmp_path / "my_source.py"
    plugin.write_text(
        "from vedun import catalog as K\n"
        "class S:\n"
        "    name = 'folder'\n"
        "    def fetch(self):\n"
        "        c = K.Catalog(); c.entries['1-1'] = K.CatalogEntry('1-1', 'T', 'x', 'May 2020', '2020-05'); return c\n"
        "    def download(self, entry, dest):\n"
        "        dest.write_bytes(b'%PDF-x'); return 6\n"
        "SOURCE = S()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(C, "SOURCE_PLUGIN", plugin)
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(K, "_loaded", {})
    src = K.load_source()
    assert src.name == "folder" and K.load_source() is src  # imported once
    assert list(K.fetch_catalog(force=True).entries) == ["1-1"]
    plugin.write_text("SOURCE = object()\n", encoding="utf-8")
    monkeypatch.setattr(K, "_loaded", {})
    with pytest.raises(K.NoSourceError, match="fetch"):
        K.load_source()


def test_profile_resolves_the_plugin_next_to_the_file_that_names_it(tmp_path, monkeypatch):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "src.py").write_text("SOURCE = None\n", encoding="utf-8")
    (tmp_path / "private" / "base.json").write_text('{"source_plugin": "src.py"}', encoding="utf-8")
    (tmp_path / "local.json").write_text('{"extends": "private/base.json"}', encoding="utf-8")
    assert C._read_config(tmp_path / "local.json")["source_plugin"] == str((tmp_path / "private" / "src.py").resolve())
    (tmp_path / "bad.json").write_text('{"source_plugin": "missing.py"}', encoding="utf-8")
    for name in ("SOURCE_PLUGIN", "CONFIG_PATH"):  # restored after the test
        monkeypatch.setattr(C, name, getattr(C, name))
    monkeypatch.setenv("VEDUN_CONFIG", str(tmp_path / "bad.json"))
    with pytest.raises(ValueError, match="source_plugin"):
        C.load_settings()


def test_download_validates_pdf_and_never_deletes(tmp_path):
    cat = fake_catalog("21-4")
    old = tmp_path / "21-4_202510.pdf"
    old.write_bytes(b"%PDF-old")
    src = FakeSource(pdf=b"%PDF-1.7 new")
    rep = K.download_updates(tmp_path, catalog=cat, only=["21-4"], source=src)
    assert [d.doc_id for d in rep.downloaded] == ["21-4"] and not rep.errors and src.downloads == ["21-4"]
    new = tmp_path / rep.downloaded[0].file
    assert new.name == "21-4_202610.pdf" and rep.downloaded[0].replaces == [old.name]
    assert old.exists()  # the old edition stays until the caller retires it
    assert not list(tmp_path.glob("*.part"))
    assert K.retire_old_pdfs(tmp_path, "21-4", new.name) == [old.name]
    assert not old.exists() and new.exists()


def test_download_rejects_a_non_pdf_and_keeps_going(tmp_path):
    cat = fake_catalog("21-4")
    (tmp_path / "21-4_202510.pdf").write_bytes(b"%PDF-old")
    (tmp_path / "22-6_202504.pdf").write_bytes(b"%PDF-old")
    src = FakeSource(pdf=[b"<html>login</html>", b"%PDF-ok"])
    rep = K.download_updates(tmp_path, catalog=cat, source=src)
    assert len(rep.errors) == 1 and len(rep.downloaded) == 1 and len(src.downloads) == 2
    assert not list(tmp_path.glob("*.part"))


def test_fill_source_item_ids(server_kb):
    from vedun import db as D

    cat = fake_catalog("9-9")
    conn = D.connect()
    assert K.fill_source_item_ids(conn, cat) == 1
    assert (
        conn.execute("SELECT source_item_id FROM documents WHERE doc_id='9-9'").fetchone()[0]
        == cat.entries["9-9"].item_id
    )
    assert K.fill_source_item_ids(conn, cat) == 0  # idempotent
    conn.close()


def test_source_name_is_the_plugin_name_or_none(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SOURCE_PLUGIN", None)
    assert K.source_name() is None
    plugin = tmp_path / "named_src.py"
    plugin.write_text(
        "class _S:\n    name = 'example.org'\n    def fetch(self): ...\n    def download(self, entry, dest): ...\n"
        "SOURCE = _S()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(C, "SOURCE_PLUGIN", plugin)
    assert K.source_name() == "example.org"
    plugin_bad = tmp_path / "broken_src.py"
    plugin_bad.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    monkeypatch.setattr(C, "SOURCE_PLUGIN", plugin_bad)
    assert K.source_name() is None  # a broken plugin must not break page rendering
