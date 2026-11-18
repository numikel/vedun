"""Example document source for vedun: a JSON catalogue published at a URL.

A source plugin lets vedun check which documents have a newer edition and download them (dashboard: Sync ->
"Fetch newer editions"). Copy this file next to your profile, change CATALOG_URL and ``name``, and point the
profile at it:

    data/vedun.config.json   {"source_plugin": "source_plugin.py"}

The catalogue this example reads is a JSON list with one object per document:

    [
      {"id": "4-12", "title": "Ladder Inspection", "published": "October 2026",
       "pdf": "https://example.org/standards/4-12.pdf"}
    ]

``id`` must match the profile's ``doc_id_pattern``; ``published`` is "<Month> <YYYY>" and becomes the edition. If your
publisher has another format (an HTML list, an API with paging, a login), keep the two methods and change their
bodies: vedun only needs ``fetch()`` to return a Catalog and ``download()`` to write one PDF.

The plugin is ordinary Python code that vedun imports: use only files you wrote or trust, and respect the
publisher's terms of use (many forbid automated downloads).
"""

from __future__ import annotations

from pathlib import Path

from vedun.catalog import Catalog, CatalogEntry, edition_from_published, http_download, http_json

CATALOG_URL = "https://example.org/standards/catalog.json"


class JsonCatalogSource:
    """Reads the catalogue with one request; downloads one PDF per call (vedun calls it sequentially)."""

    name = "example.org"  # shown in the dashboard: "Fetch newer editions from example.org"

    def __init__(self, url: str = CATALOG_URL) -> None:
        self.url = url

    def fetch(self) -> Catalog:
        """The current list of documents and editions. Raise on any error: vedun keeps the previous snapshot."""
        cat = Catalog()
        for item in http_json(self.url):
            published = item.get("published")
            cat.entries[item["id"]] = CatalogEntry(
                doc_id=item["id"],
                title=item.get("title", ""),
                item_id=item["pdf"],  # whatever download() needs to find the file: here its URL
                published=published,
                edition=edition_from_published(published),
            )
        return cat

    def download(self, entry: CatalogEntry, dest: Path) -> int:
        """Write the PDF to ``dest`` and return its size; http_download checks the %PDF header and a size cap."""
        return http_download(entry.item_id, dest)


SOURCE = JsonCatalogSource()
