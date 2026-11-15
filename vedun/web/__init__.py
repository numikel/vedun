"""Server-rendered web UI (Jinja2 + HTMX) for the vedun knowledge base.

``from vedun.web import router, STATIC_DIR`` - include the router and mount ``STATIC_DIR`` at ``/static``.
"""

from .routes import STATIC_DIR, router

__all__ = ["STATIC_DIR", "router"]
