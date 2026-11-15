"""MCP server of the vedun knowledge base: ``build_mcp_asgi()`` is what :mod:`vedun.server.app` mounts at ``/mcp``.

Protocol: MCP revision **2026-07-28** (stateless, ``server/discover``, per-request ``_meta``), served by the official
Python SDK (``mcp`` 2.x, ``MCPServer``). The same app also answers handshake-era clients (``initialize``,
2025-11-25 and older) because the SDK routes every request by its ``MCP-Protocol-Version`` header.

Transport choices (spec ``basic/transports/streamable-http``):

* ``stateless_http=True``: the 2026-07-28 leg has no sessions by construction; the flag only affects the legacy leg
  and removes its in-process session table. None of our tools needs a server-to-client request (sampling,
  elicitation, roots), so nothing is lost, a restart of the service never invalidates a client, and any worker can
  answer any request.
* ``json_response=True``: every POST is answered with one JSON body; no tool streams progress.
* ``subscriptions=False``: the tools never change at run time, so no ``subscriptions/listen`` stream is advertised
  (Claude Code would otherwise hold one open); freshness is handled by ``ttlMs`` on ``server/discover`` / ``tools/list``.
* DNS-rebinding guard (spec MUST: validate ``Origin``; SHOULD: bind to localhost): the SDK checks ``Host`` and ``Origin``
  against the same allowlists as the rest of the service (``VEDUN_ALLOWED_HOSTS`` / ``VEDUN_ALLOWED_ORIGINS``).
* Authentication, rate limit and audit are NOT done here: :class:`vedun.server.security.SecurityMiddleware` runs in
  front of ``/mcp`` (API key as ``Authorization: Bearer`` or ``X-API-Key``). See the README, "Authorization".
"""

from __future__ import annotations

from typing import Any

from mcp.server import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse

from vedun import config as C

from . import mcp_tools
from . import settings as S

SERVER_NAME = "vedun"
PROTOCOL_REVISION = "2026-07-28"  # older handshake-era clients (initialize, 2025-11-25 and before) also work

INSTRUCTIONS = """\
Local, read-only knowledge base of {corpus} (one edition per document number).
Choose the tool by what is already known:
- document AND clause/table/figure known -> kb_lookup (exact, free)
- what clauses exist under a document/branch -> kb_toc
- exact words, numbers with units, boolean/proximity -> kb_fulltext (free; English corpus)
- a descriptive question, Polish or English, document unknown -> kb_search (paid APIs, daily budget, falls back to BM25)
- read a whole document or chapter, or get the PDF/Markdown file paths -> kb_get_document
- a figure or a printed page as an image -> kb_figure, kb_page_image
- before a quotation goes into a mail, opinion or report -> kb_verify_quote; for a whole draft -> kb_check_citations
- which documents and editions exist, and whether the document source has newer ones -> kb_documents
Citations: copy citation_pl / citation_en from the result exactly as given (edition included, no page number).
Clause numbers change between editions, so an id remembered from an old note is not evidence until looked up.
Results are paged: when next_cursor is set, call the same tool again with the same arguments plus cursor.
Not found (found=false) is a normal answer with the nearest ids; it is not a reason to guess an id.
"""

#: tool list and discover answers do not change while the process runs; 5 min / 1 h are conservative freshness hints
CACHE_HINTS: dict[str, CacheHint] = {
    "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
    "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
}

MAX_REQUEST_BODY = 256 * 1024  # largest legitimate request: kb_check_citations with 50 000 characters


def build_mcp_server() -> MCPServer:
    """A fresh server object with all tools registered (a new one per app, so tests do not share state)."""
    server = MCPServer(
        SERVER_NAME,
        title=f"vedun: {C.CORPUS_NAME}",
        description=f"Exact, cited access to {C.CORPUS_NAME}",
        instructions=mcp_tools.localize(INSTRUCTIONS.format(corpus=C.CORPUS_NAME)),
        version=S.VERSION,
        subscriptions=False,
        cache_hints=CACHE_HINTS,  # type: ignore[arg-type]
        log_level="WARNING",
    )
    mcp_tools.register_tools(server)
    return server


def transport_security() -> TransportSecuritySettings:
    """Host / Origin allowlists derived from the service settings (``localhost`` family, the configured port)."""
    hosts: list[str] = []
    for h in S.allowed_hosts():
        hosts += [h, f"{h}:*"]
    origins = [f"http://{o}" for o in sorted(S.allowed_origin_hosts())]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


class _PostOnly:
    """405 for everything but POST (spec 2026-07-28: no GET stream, no sessions to DELETE).

    Handshake-era clients that probe ``GET /mcp`` for a standalone SSE stream get the answer the 2025-03-26 spec allows
    for a server without one, instead of a request that the stateless SDK would keep open indefinitely.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] != "POST":
            await PlainTextResponse("POST only: this server keeps no stream or session", 405, {"Allow": "POST"})(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


class _RootAlias:
    """ASGI app answering the exact path ``/mcp`` (Starlette's ``Mount`` only matches ``/mcp/...``).

    Without it ``POST /mcp`` gets a 307 redirect to ``/mcp/``: a second round trip, counted twice by the rate limiter,
    and a client that does not replay the body on a redirect would fail.
    """

    def __init__(self, inner: Starlette, prefix: str = "/mcp") -> None:
        self.inner, self.prefix = inner, prefix

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        root = scope.get("root_path", "")
        scope = dict(
            scope,
            path=f"{root}{self.prefix}/",
            raw_path=f"{root}{self.prefix}/".encode(),
            root_path=f"{root}{self.prefix}",
        )
        await self.inner(scope, receive, send)


def build_mcp_asgi() -> Starlette:
    """The Streamable HTTP app. Mount it at ``/mcp``; its ``router.lifespan_context`` must be entered by the host app.

    The returned object carries ``root_alias``: an ASGI app for the exact path ``/mcp`` (see :class:`_RootAlias`).
    """
    sub = build_mcp_server().streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY,
        transport_security=transport_security(),
    )
    sub.add_middleware(_PostOnly)
    sub.root_alias = _RootAlias(sub)  # type: ignore[attr-defined]
    return sub
