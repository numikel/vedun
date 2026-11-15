"""Whole-document access: Markdown (full, by section, paginated), PDF, host paths, guards, dashboard links."""

from __future__ import annotations

import pytest

from tests.helpers_server import BASIC
from vedun import config as C
from vedun import db as D
from vedun import documents as docs

HOST_PDF = "D:/lib/acme/acme_documents"
HOST_ROOT = "D:/lib/acme/vedun"


@pytest.fixture()
def long_md(server_kb):
    """The sheet's Markdown grown to ~40 kB so that pagination has several pages."""
    md = C.MD_DIR / "9-9_202610.md"
    md.write_text(
        md.read_text(encoding="utf-8")
        + "\n\n"
        + "\n\n".join(f"Paragraph {i}. " + "Lorem ipsum dolor sit amet. " * 12 for i in range(120)),
        encoding="utf-8",
    )
    return md


def test_meta_has_structure_and_paths(client, monkeypatch):
    monkeypatch.setenv("VEDUN_HOST_PDF_DIR", HOST_PDF)
    monkeypatch.setenv("VEDUN_HOST_ROOT", HOST_ROOT)
    d = client.get("/api/documents/9-9").json()
    assert d["indexed"] and d["title"] == "Test Sheet" and d["units"]["clause"] > 0
    assert d["citation_prefix"].startswith("ASC 9-9 „Test Sheet”") and not d["citation_prefix"].endswith("pkt")
    assert [t["unit_id"] for t in d["toc"]][:2] == ["1.0", "2.0"]
    p = d["paths"]
    assert p["pdf_path_host"] == f"{HOST_PDF}/9-9_202610.pdf"
    assert p["md_path_host"] == f"{HOST_ROOT}/md/9-9_202610.md"
    assert (
        p["pdf_path_container"].replace("\\", "/").endswith("/pdf/9-9_202610.pdf")
        and p["pdf_exists"]
        and p["md_exists"]
    )


def test_host_path_equals_local_path_when_running_on_the_host(client):
    p = client.get("/api/documents/9-9").json()["paths"]
    assert p["pdf_path_host"] == p["pdf_path_container"] and "\\" not in p["md_path_host"]


def test_documents_list_includes_pdf_only_sheets(client, server_kb):
    (server_kb.pdf_dir / "1-1_202601.pdf").write_bytes(b"%PDF-1.7 not indexed")
    rows = {r["doc_id"]: r for r in client.get("/api/documents").json()["documents"]}
    assert rows["9-9"]["indexed"] and rows["9-9"]["md_exists"]
    assert rows["1-1"]["indexed"] is False and rows["1-1"]["pdf_exists"] and not rows["1-1"]["md_exists"]
    assert client.get("/api/documents/1-1").json()["indexed"] is False
    assert client.get("/api/documents/1-1.md").status_code == 404  # no Markdown without an index


def test_markdown_full_and_by_section(client):
    full = client.get("/api/documents/9-9.md")
    assert full.status_code == 200 and full.headers["content-type"].startswith("text/markdown")
    assert "SCOPE" in full.text and "45 kg (99 lb)" in full.text
    assert full.headers["x-total-chars"] == str(len(full.text)) and full.headers["x-next-offset"] == ""
    sec = client.get("/api/documents/9-9.md", params={"section": "2.3"})
    assert "2.3.2.1" in sec.text and "45 kg (99 lb)" in sec.text and "SCOPE" not in sec.text
    one = client.get("/api/documents/9-9.md", params={"section": "pkt 2.3.2.4"}).text
    assert "45 kg (99 lb)" in one and "2.3.2.1" not in one
    assert client.get("/api/documents/9-9.md", params={"section": "7.7.7"}).status_code == 404


def test_pagination_is_contiguous_and_concatenates_to_the_full_text(client, long_md):
    full = client.get("/api/documents/9-9.md", params={"full": "true"})
    assert "attachment" in full.headers["content-disposition"] and len(full.text) > 30_000
    pages, offset = [], 0
    while offset is not None:
        r = client.get("/api/documents/9-9.md", params={"offset": offset, "max_chars": 5000})
        assert r.status_code == 200 and len(r.text) <= 5000
        pages.append(r.text)
        nxt = r.headers["x-next-offset"]
        assert int(r.headers["x-total-chars"]) == len(full.text)
        if nxt:
            assert int(nxt) == offset + len(r.text)  # no gap, no overlap
            assert r.text.endswith("\n\n") or len(r.text) > 2500  # cut on a blank line where possible
        offset = int(nxt) if nxt else None
    assert len(pages) > 5 and "".join(pages) == full.text


def test_pagination_inside_a_section_and_library_contract(server_kb, long_md):
    conn = D.connect()
    a = docs.get_document(conn, "9-9", "md", section="2.3", max_chars=1000)
    assert a["found"] and a["section"] == "2.3" and a["total_chars"] < 1500 and a["offset"] == 0
    whole = docs.get_document(conn, "9-9", "md", max_chars=1000)
    assert whole["next_offset"] == whole["returned_chars"] or whole["next_offset"] > 0
    assert whole["total_chars"] == len(long_md.read_text(encoding="utf-8"))
    last = docs.get_document(conn, "9-9", "md", offset=whole["total_chars"] - 10, max_chars=1000)
    assert last["next_offset"] is None and len(last["text"]) == 10
    pdf = docs.get_document(conn, "9-9", "pdf")
    assert pdf["url"] == "/api/documents/9-9.pdf" and "text" not in pdf  # never bytes
    assert docs.get_document(conn, "9-9", "meta")["found"]
    with pytest.raises(ValueError):
        docs.get_document(conn, "../9-9", "md")
    with pytest.raises(ValueError):
        docs.get_document(conn, "9-9", "docx")  # type: ignore[arg-type]
    conn.close()


def test_pdf_endpoint_returns_the_original(client):
    r = client.get("/api/documents/9-9.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and r.headers["content-disposition"].startswith("inline")


@pytest.mark.parametrize(
    "path",
    [
        "/api/documents/8-8",
        "/api/documents/8-8.md",
        "/api/documents/8-8.pdf",
        "/api/documents/..%2F..%2Fetc%2Fpasswd.pdf",
        "/api/documents/..%5C..%5Cx.md",
        "/api/documents/9-9%00.pdf",
        "/api/documents/9-9.md?section=../../../etc/passwd",
        "/api/documents/%2e%2e/9-9.pdf",
    ],
)
def test_unknown_sheets_and_traversal_are_refused(client, path):
    r = client.get(path)
    assert r.status_code in (400, 404, 422)
    assert "root:" not in r.text


def test_documents_need_credentials_but_no_csrf_header(raw_client):
    assert raw_client.get("/api/documents/9-9").status_code == 401
    assert raw_client.get("/api/documents/9-9.pdf", headers=BASIC).status_code == 200  # read-only GET: no header needed


def test_sheet_page_links_to_markdown_and_pdf(raw_client, monkeypatch):
    monkeypatch.setenv("VEDUN_HOST_PDF_DIR", HOST_PDF)
    monkeypatch.setenv("VEDUN_HOST_ROOT", HOST_ROOT)
    page = raw_client.get("/documents/9-9", headers=BASIC)
    assert page.status_code == 200
    t = page.text
    assert "Pobierz Markdown" in t and 'href="/api/documents/9-9.md?full=true"' in t
    assert "Otwórz PDF (oryginał)" in t and 'href="/api/documents/9-9.pdf"' in t
    assert f'data-copy="{HOST_PDF}/9-9_202610.pdf"' in t and f'data-copy="{HOST_ROOT}/md/9-9_202610.md"' in t
