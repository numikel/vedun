"""Local knowledge base over a collection of technical documents (PDF -> Markdown -> SQLite FTS5 + vectors).

Library entry points (one per future MCP tool), import them from their modules::

    from vedun.db import connect, sync, replace_document, remove_document, doctor
    from vedun.search import lookup, toc, fulltext, search, figure, page_image, list_documents
    from vedun.quote import verify_quote
    from vedun.check_citations import check_citations
"""

__version__ = "0.1.2"
