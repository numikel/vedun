"""Authentication, rate limiting and the cross-site guard.

Layers (outermost first, see :func:`vedun.server.app.create_app`):

1. ``TrustedHostMiddleware``: wrong ``Host`` -> 400 (DNS rebinding).
2. :class:`SecurityMiddleware` (pure ASGI):
   * ``/api/health`` is open (Docker healthcheck, leaks nothing);
   * rate limit per credential / client address on ``/api`` and ``/mcp`` (429 + ``Retry-After``);
   * fail closed: when ``VEDUN_API_KEY``, ``VEDUN_DASH_USER`` or ``VEDUN_DASH_PASSWORD`` is unset every protected
     route answers 503;
   * ``/mcp``: ``Authorization: Bearer <VEDUN_API_KEY>`` only;
   * ``/api``: Bearer, or HTTP Basic (dashboard credentials, so the page's own ``fetch()`` works), or the
     per-process internal token that the dashboard's in-process client sends;
   * everything else (dashboard pages, ``/static``, ``/media``): HTTP Basic.
   Secrets are compared with :func:`secrets.compare_digest` and never logged.
3. :func:`require_same_origin`, a FastAPI dependency on every state-changing ``/api`` route and on ``GET /api/search``
   (which spends money): browsers attach cached Basic credentials to cross-site requests, so credentials alone
   do not stop CSRF. The custom header cannot be sent cross-site without a CORS preflight, which the API never grants.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import HTTPException, Request

from . import audit
from . import settings as S

#: random per process, in memory only; the in-process dashboard client sends it, nobody else knows it
INTERNAL_TOKEN = secrets.token_urlsafe(32)
INTERNAL_HEADER = "x-vedun-internal"


def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _area(path: str) -> str:
    if path == "/api/health":
        return "health"
    if path == "/api" or path.startswith("/api/"):
        return "api"
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    return "dash"


class RateLimiter:
    """Sliding one-minute window per key (in memory, per process)."""

    def __init__(self, per_min: int | None = None) -> None:
        self.per_min = per_min
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> int:
        """0 when the request may pass, else the seconds to wait."""
        limit = self.per_min if self.per_min is not None else S.rate_limit_per_min()
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= limit:
            return max(1, int(60 - (now - q[0])) + 1)
        q.append(now)
        if len(self._hits) > 5000:  # forget idle keys
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= 60]:
                del self._hits[k]
        return 0


class SecurityMiddleware:
    def __init__(self, app: Any, limiter: RateLimiter | None = None) -> None:
        self.app = app
        self.limiter = limiter or RateLimiter()
        self._last_fail: dict[str, float] = {}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":  # none is served; refuse instead of passing around the auth
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        area = _area(scope["path"])
        if area == "health":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        client = (scope.get("client") or ("?", 0))[0]
        internal = (
            area == "api" and headers.get(INTERNAL_HEADER) is not None and _eq(headers[INTERNAL_HEADER], INTERNAL_TOKEN)
        )

        if area in ("api", "mcp") and not internal:
            cred = auth or headers.get("x-api-key", "")
            key = hashlib.sha256(cred.encode()).hexdigest()[:16] if cred else f"ip:{client}"
            wait = self.limiter.check(key)
            if wait:
                audit.log_event("rate_limited", outcome=f"429 {area}", caller="unknown")
                await self._reply(
                    send,
                    429,
                    {"error": {"code": "rate_limited", "message": f"too many requests, retry in {wait} s"}},
                    {"Retry-After": str(wait)},
                )
                return
        missing = S.missing_secrets()
        if missing:
            await self._reply(
                send,
                503,
                {
                    "error": {
                        "code": "auth_not_configured",
                        "message": "authentication is not configured on the server ("
                        + ", ".join(missing)
                        + " unset); refusing to serve",
                    }
                },
            )
            return

        caller = self._authenticate(area, auth, internal, headers.get("x-api-key", ""))
        if caller is None:
            self._audit_failure(client, area, bool(auth))
            if area == "dash":
                www = 'Basic realm="vedun", charset="UTF-8"'
            else:
                www = "Bearer"
            await self._reply(
                send,
                401,
                {"error": {"code": "unauthorized", "message": "authentication required"}},
                {"WWW-Authenticate": www},
            )
            return
        token = audit.CALLER.set(caller)
        try:
            await self.app(scope, receive, send)
        finally:
            audit.CALLER.reset(token)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _authenticate(area: str, auth: str, internal: bool, api_key: str = "") -> str | None:
        if internal:
            return "internal"
        if api_key and area in ("api", "mcp") and _eq(api_key.strip(), S.secret("API_KEY")):
            return "mcp" if area == "mcp" else "api"  # X-API-Key: the same key as Bearer
        scheme, _, value = auth.partition(" ")
        scheme = scheme.lower()
        if scheme == "bearer" and area in ("api", "mcp"):
            return ("mcp" if area == "mcp" else "api") if _eq(value.strip(), S.secret("API_KEY")) else None
        if scheme == "basic" and area in ("api", "dash"):
            try:
                user, _, pw = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
            except Exception:  # noqa: BLE001 - malformed header
                return None
            ok_user = _eq(user, S.secret("DASH_USER"))
            ok_pw = _eq(pw, S.secret("DASH_PASSWORD"))  # both always evaluated: no early exit on the user name
            return "dashboard" if ok_user and ok_pw else None
        return None

    def _audit_failure(self, client: str, area: str, had_credentials: bool) -> None:
        now = time.monotonic()
        if now - self._last_fail.get(client, -99) < 10:  # one entry per client per 10 s: a flood cannot fill the log
            return
        self._last_fail[client] = now
        audit.log_event(
            "auth_failure",
            outcome=f"denied {area}",
            detail="bad credentials" if had_credentials else "no credentials",
            caller="unknown",
        )

    @staticmethod
    async def _reply(send: Any, status: int, body: dict, extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(body).encode()
        hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
        hdrs += [(k.lower().encode(), v.encode()) for k, v in (extra or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": raw})


def require_same_origin(request: Request) -> None:
    """Dependency for state-changing and paid routes: reject cross-site requests with 403.

    Needs ``X-Requested-With: vedun`` or ``HX-Request: true``; ``Sec-Fetch-Site`` absent /
    ``same-origin`` / ``none``; and, when an ``Origin`` header is present, its host:port in ``VEDUN_ALLOWED_ORIGINS``
    (default 127.0.0.1 / localhost on the configured port).
    """
    h = request.headers
    problem = None
    if h.get("x-requested-with") != "vedun" and h.get("hx-request", "").lower() != "true":
        problem = "missing header X-Requested-With: vedun"
    elif h.get("sec-fetch-site") not in (None, "same-origin", "none"):
        problem = f"cross-site request (Sec-Fetch-Site: {h.get('sec-fetch-site')})"
    elif (origin := h.get("origin")) is not None:
        host = origin.split("://", 1)[-1].lower()
        if host not in S.allowed_origin_hosts():
            problem = "Origin not allowed"
    if problem:
        audit.log_event("csrf_denied", outcome=f"403 {request.method} {request.url.path}", detail=problem)
        raise HTTPException(403, f"cross-site request refused: {problem}")
