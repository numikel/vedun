"""Corpus profile: neutral defaults, validation, ``extends`` (real collections keep their values in a private profile)."""

from __future__ import annotations

import json

import pytest

from vedun import config as C


@pytest.fixture()
def reload(tmp_path, monkeypatch):
    """Load a given config dict (or none) with fresh module defaults; the test profile is restored afterwards."""
    names = [n for n in dir(C) if n.isupper()]
    saved = {n: getattr(C, n) for n in names}
    monkeypatch.setattr(C, "MODELS", dict(C.MODELS))

    def load(cfg: dict | None, *, base: dict | None = None):
        for n in ("DOC_LABEL", "CITATION_TEMPLATE", "LOCATORS", "EDITION_LABEL", "TITLE_STRIP_PATTERNS"):
            monkeypatch.setattr(C, n, DEFAULTS[n])
        monkeypatch.setattr(C, "DEFAULT_RERANKER", None)
        path = tmp_path / "kb.json"
        if base is not None:
            (tmp_path / "base.json").write_text(json.dumps(base), encoding="utf-8")
            cfg = {"extends": "base.json", **(cfg or {})}
        path.write_text(json.dumps(cfg or {}), encoding="utf-8")
        monkeypatch.setenv("VEDUN_CONFIG", str(path))
        C.load_settings()

    yield load
    for n, v in saved.items():
        setattr(C, n, v)


DEFAULTS = {
    "DOC_LABEL": "{doc_id}",
    "CITATION_TEMPLATE": {
        "pl": "{doc_id} „{title}” ({edition}), {locator}",
        "en": '{doc_id} "{title}" ({edition}), {locator}',
    },
    "LOCATORS": {
        "pl": {"clause": "pkt {id}", "table": "Tab. {id}", "figure": "rys. {id}", "appendix": "zał. {id}"},
        "en": {"clause": "Section {id}", "table": "Table {id}", "figure": "Fig. {id}", "appendix": "Appendix {id}"},
    },
    "EDITION_LABEL": None,
    "TITLE_STRIP_PATTERNS": (),
}


def test_neutral_defaults_carry_no_corpus_names(reload, tmp_path):
    from tests.helpers import make_pdf
    from vedun.convert import edition_label, pdf_title
    from vedun.evidence import make_citation

    reload({})
    assert C.DEFAULT_RERANKER is None and C.doc_label("21-4") == "21-4"
    assert (
        make_citation("21-4", "Storage", None, "2026-10", "table", "Table 2.1", "pl")
        == "21-4 „Storage” (2026-10), Tab. 2.1"
    )
    pdf = make_pdf(tmp_path / "21-4_202610.pdf", [(50, 60, "October 2026")], title="ASC 21-4 Storage (Code Part)")
    assert edition_label(pdf, "2026-10")[1].startswith("derived from file name")  # no rules: file name, even if printed
    assert pdf_title(pdf, "21-4") == "ASC 21-4 Storage (Code Part)"  # nothing stripped without a profile


def test_profile_drives_citation_label_and_title(reload, tmp_path):
    from tests.helpers import make_pdf
    from vedun.convert import edition_label, pdf_title
    from vedun.evidence import make_citation

    base = {
        "doc_label": "STD {doc_id}",
        "citation_template": {"en": 'STD {doc_id} "{title}" ({edition}), {locator}'},
        "locators": {"en": {"clause": "cl. {id}"}},
        "edition_label": {"original": "{MONTH}", "revision": "Amended {MONTH}"},
        "title_strip_patterns": [r"^STD\s+{doc_id}\s+"],
    }
    reload({"corpus_name": "Standards"}, base=base)  # via extends; local keys add to the base
    assert C.CORPUS_NAME == "Standards" and C.doc_label("1-2") == "STD 1-2"
    assert make_citation("1-2", "T", "May 2020", "2020-05", "clause", "4.1", "en") == 'STD 1-2 "T" (May 2020), cl. 4.1'
    pdf = make_pdf(
        tmp_path / "1-2_202605.pdf", [(50, 60, "May 2020"), (50, 74, "Amended May 2026")], title="STD 1-2 Ladders"
    )
    assert edition_label(pdf, "2026-05") == ("May 2020, Amended May 2026", "printed on the sheet")
    assert pdf_title(pdf, "1-2") == "Ladders"


@pytest.mark.parametrize(
    "bad",
    [
        {"citation_template": {"en": "{doc_id} {titel}"}},  # typo in a field name
        {"locators": {"en": {"clause": "cl. {num}"}}},
        {"locators": {"en": {"chapter": "ch. {id}"}}},  # unknown unit kind
        {"edition_label": {"revision": "Amended ({MONTH}"}},
        {"doc_label": "{doc}"},
    ],
)
def test_profile_errors_stop_at_load(reload, bad):
    with pytest.raises(ValueError):
        reload(bad)


def test_extends_loop_is_an_error(tmp_path, monkeypatch):
    (tmp_path / "a.json").write_text(json.dumps({"extends": "b.json"}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"extends": "a.json"}), encoding="utf-8")
    with pytest.raises(ValueError, match="loop"):
        C._read_config(tmp_path / "a.json")


def test_env_reads_vedun_variables_and_treats_empty_as_unset(monkeypatch):
    monkeypatch.setenv("VEDUN_PORT", "9002")
    assert C.env("PORT") == "9002"
    monkeypatch.setenv("VEDUN_PORT", "")  # compose passes unset secrets as ""
    assert C.env("PORT", "8765") == "8765"
    monkeypatch.delenv("VEDUN_PORT")
    assert C.env("PORT") is None


def test_profile_adds_folders_that_check_citations_skips(tmp_path, monkeypatch):
    from vedun.check_citations import iter_text_files

    for d in ("notes", "Library PDFs", "vedun", ".git"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(C, "CITATION_SKIP_DIRS", ())
    assert sorted(p.parent.name for p in iter_text_files(tmp_path)) == ["Library PDFs", "notes"]
    monkeypatch.setattr(C, "CITATION_SKIP_DIRS", ("Library PDFs",))
    assert [p.parent.name for p in iter_text_files(tmp_path)] == ["notes"]
