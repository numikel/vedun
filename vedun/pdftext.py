"""Plain text and metadata of a PDF via pypdfium2 (Apache-2.0/BSD): one place for every text read from a source PDF."""

from __future__ import annotations

from pathlib import Path


def page_texts(path: Path, max_pages: int | None = None) -> list[str]:
    """Text of each page in reading order, ``\\n`` line ends (pdfium returns ``\\r\\n``)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        n = len(doc) if max_pages is None else min(len(doc), max_pages)
        out = []
        for i in range(n):
            page = doc[i]
            tp = page.get_textpage()
            try:
                t = tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
                # pdfium marks a hyphen at a line break as U+FFFE (or \x02): restore "-\n" as printed
                out.append(t.replace("￾", "-\n").replace("\x02", "-\n"))
            finally:
                tp.close()
                page.close()
        return out
    finally:
        doc.close()


def page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        return len(doc)
    finally:
        doc.close()


def metadata_title(path: Path) -> str:
    """The document-information ``Title`` of the PDF, or ``""``."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        return str(doc.get_metadata_dict().get("Title") or "").strip()
    finally:
        doc.close()
