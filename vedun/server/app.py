"""FastAPI application: REST API (``/api``), optional dashboard (``/``, ``/static``) and optional MCP (``/mcp``).

Run on the host:       ``python -m uvicorn vedun.server.app:app --host 127.0.0.1 --port 8765``
Run in the container:  see ``compose.yaml`` (uvicorn, 1 worker; the job queue is in-process).

The dashboard and the MCP server are plugged in by import so that each track can be developed alone:
a missing or broken module is logged and skipped, it never takes the API down.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import Route

from vedun import config as C

from . import api, jobs, security
from . import settings as S

log = logging.getLogger("vedun.server")

_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    413: "too_large",
    422: "validation_error",
    503: "unavailable",
}


#: A running ``sync`` holds the write lock while it commits a sheet; readers then get 503, not 500.
BUSY_MESSAGE = "The knowledge base is busy writing a document (sync running). Retry in a few seconds."
BUSY_RETRY_AFTER_S = 5


def _envelope(status: int, message: str, **extra: Any) -> JSONResponse:
    code = _CODES.get(status, "error")
    err = {"code": code, "message": message, "status": status, **extra}
    return JSONResponse({"error": err, "detail": message}, status_code=status)


def _internal_api_get(app: Any, path: str, timeout: float = 4.0) -> Any:
    """The dashboard's in-process ``GET /api/...`` (installed as ``vedun.web.data.hooks.api_get``).

    Same job as ``web.data.api_get`` but it presents the per-process internal token, so the dashboard needs no
    external credential and nothing outside the process can use that path.
    """
    import asyncio

    import httpx

    async def _go() -> Any:
        transport = httpx.ASGITransport(app=app)
        headers = {security.INTERNAL_HEADER: security.INTERNAL_TOKEN}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://vedun.internal", timeout=timeout, headers=headers
        ) as c:
            r = await c.get(path)
            return r.json() if r.status_code == 200 else None

    try:
        import anyio.from_thread

        return anyio.from_thread.run(_go)
    except RuntimeError:  # not inside a worker thread
        return asyncio.run(_go())


def _mount_optional(app: FastAPI, stack_holder: list[Any]) -> None:
    """Plug in the dashboard and the MCP server if their modules import cleanly."""
    try:
        from starlette.staticfiles import StaticFiles

        from vedun.web import STATIC_DIR
        from vedun.web import data as web_data
        from vedun.web import router as web_router

        if web_data.hooks.api_get is None:  # tests may have installed their own
            web_data.hooks.api_get = _internal_api_get
        app.include_router(web_router)
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        log.info("dashboard mounted (/ and /static)")
    except Exception as e:  # noqa: BLE001 - optional track
        log.warning("dashboard not mounted: %s: %s", type(e).__name__, e)
    try:
        mcp_app = importlib.import_module("vedun.server.mcp_app")
        build = getattr(mcp_app, "build_mcp_asgi", None)
        if build is None:
            log.info("vedun.server.mcp_app has no build_mcp_asgi(): MCP not mounted")
        else:
            sub = build()
            app.mount("/mcp", sub)
            if (alias := getattr(sub, "root_alias", None)) is not None:  # "/mcp" without the slash: no 307 round trip
                app.router.routes.append(Route("/mcp", endpoint=alias))
            stack_holder.append(sub)  # its lifespan (e.g. the MCP session manager) is entered in our lifespan
            log.info("MCP mounted at /mcp")
    except ModuleNotFoundError:
        log.info("vedun.server.mcp_app not present: MCP not mounted")
    except Exception as e:  # noqa: BLE001
        log.warning("MCP not mounted: %s: %s", type(e).__name__, e)


def create_app() -> FastAPI:
    subapps: list[Any] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        S.ensure_db()  # before the job worker: the only moment nothing else writes
        jobs.start()
        async with AsyncExitStack() as stack:
            for sub in subapps:  # Starlette sub-apps keep their lifespan in router.lifespan_context
                ctx = getattr(getattr(sub, "router", None), "lifespan_context", None)
                if ctx is not None:
                    await stack.enter_async_context(ctx(sub))
            yield
        jobs.stop()

    app = FastAPI(
        title=f"vedun: {C.CORPUS_NAME}",
        version=S.VERSION,
        description=f"Local, read-mostly knowledge base over {C.CORPUS_NAME}. Every hit is an Evidence record.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    # added first = inner: authentication, rate limit; added last = outermost: the Host check (DNS rebinding)
    app.add_middleware(security.SecurityMiddleware, limiter=security.RateLimiter())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=S.allowed_hosts())
    if missing := S.missing_dashboard_secrets():
        log.error(
            "AUTHENTICATION NOT CONFIGURED: %s unset. Every protected route answers 503 until they are set "
            "(vedun keys, or your secrets manager).",
            ", ".join(missing),
        )
    elif not S.secret("API_KEY"):
        log.warning(
            "%s unset: /api and /mcp accept only keys created on the dashboard page 'MCP connection' "
            "and answer 503 while there is none.",
            S.api_key_var(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Any:
        if request.url.path.startswith("/api"):
            return _envelope(exc.status_code, str(exc.detail))
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Any:
        if request.url.path.startswith("/api"):
            details = [{"where": ".".join(str(x) for x in e["loc"]), "problem": e["msg"]} for e in exc.errors()]
            first = details[0] if details else {"where": "?", "problem": "invalid request"}
            return _envelope(422, f"{first['where']}: {first['problem']}", details=details[:10])
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_busy(request: Request, exc: sqlite3.OperationalError) -> Any:
        msg = str(exc).lower()
        if "locked" not in msg and "busy" not in msg:
            return await _unhandled(request, exc)
        log.warning("database busy on %s: %s", request.url.path, exc)
        resp = _envelope(503, BUSY_MESSAGE)
        resp.headers["Retry-After"] = str(BUSY_RETRY_AFTER_S)
        return resp

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Any:
        log.exception("unhandled error on %s", request.url.path)
        return _envelope(500, f"internal error: {type(exc).__name__}")

    app.include_router(api.router)
    _mount_optional(app, subapps)
    return app


app = create_app()
