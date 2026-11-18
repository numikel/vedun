"""API keys issued from the dashboard: named, scoped, expiring, revocable; the value is shown once.

The master key (``VEDUN_API_KEY``, from the host's secret store) keeps every scope. Keys created on the dashboard
page *MCP connection* live in the sidecar ``state.db``; only their SHA-256 is stored, so a copy of that file does not
reveal them. Scopes:

* ``mcp:read`` — ``/mcp`` (every MCP tool is read-only; ``kb_search`` spends the daily budget);
* ``api:read`` — ``GET /api/*`` plus the two POST routes that only check text (:data:`READ_POSTS`);
* ``api:write`` — the job routes that change state or start paid work (:data:`WRITE_ROUTES`); implies ``api:read``.

Keys never manage keys: creating and revoking happens only on the dashboard (HTTP Basic). A route that is missing
from the tables below is treated as ``api:write`` and fails a test until it is classified.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..state import connect_state

SCOPES = ("mcp:read", "api:read", "api:write")
ALL_SCOPES = frozenset(SCOPES)
DEFAULT_SCOPES = frozenset({"mcp:read"})
EXPIRY_DAYS = (30, 90, None)  # None = no expiry; the dashboard offers these
DEFAULT_EXPIRY_DAYS = 90
TOKEN_PREFIX = "vk_"
NAME_MAX = 40
TOUCH_EVERY_S = 60.0  # last_used_at is written at most once a minute per key, not on every request

#: POST routes that only read or check text (no state change, no paid job)
READ_POSTS = {"/api/verify", "/api/check-citations"}
#: routes that change state or start paid work; patterns are full-match regexes over the path
WRITE_ROUTES = (
    ("POST", re.compile(r"/api/jobs/sync")),
    ("POST", re.compile(r"/api/jobs/catalog-refresh")),
    ("POST", re.compile(r"/api/jobs/check-citations")),
    ("POST", re.compile(r"/api/jobs/[^/]+/cancel")),
)


def required_scope(area: str, method: str, path: str) -> str:
    """The scope a request needs: ``mcp`` area -> ``mcp:read``; ``api`` by method and route (default ``api:write``)."""
    if area == "mcp":
        return "mcp:read"
    if method in ("GET", "HEAD", "OPTIONS"):
        return "api:read"
    if path in READ_POSTS:
        return "api:read"
    return "api:write"  # WRITE_ROUTES and anything not yet classified: the stricter scope


def normalize_scopes(scopes: set[str] | frozenset[str] | list[str]) -> frozenset[str]:
    """Known scopes only; ``api:write`` brings ``api:read`` with it."""
    s = {x for x in scopes if x in ALL_SCOPES}
    if "api:write" in s:
        s.add("api:read")
    return frozenset(s)


@dataclass(frozen=True)
class KeyRecord:
    id: int
    name: str
    prefix: str  # first characters of the value, to recognise a key in the list
    scopes: frozenset[str]
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    revoked_at: str | None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= _now()

    @property
    def active(self) -> bool:
        return self.revoked_at is None and not self.expired

    @property
    def status(self) -> str:
        return "revoked" if self.revoked_at else "expired" if self.expired else "active"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_keys(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, prefix TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT, last_used_at TEXT, revoked_at TEXT)"""
    )


def _row(r: sqlite3.Row) -> KeyRecord:
    return KeyRecord(
        id=r["id"],
        name=r["name"],
        prefix=r["prefix"],
        scopes=normalize_scopes(r["scopes"].split()),
        created_at=r["created_at"],
        expires_at=r["expires_at"],
        last_used_at=r["last_used_at"],
        revoked_at=r["revoked_at"],
    )


def _connect() -> sqlite3.Connection:
    conn = connect_state()
    _ensure(conn)
    return conn


def clean_name(name: str) -> str:
    """A label for the list: printable, single line, at most :data:`NAME_MAX` characters; empty raises."""
    n = " ".join(str(name).split())[:NAME_MAX]
    if not n:
        raise ValueError("give the key a name, e.g. the client it is for")
    return n


def create(name: str, scopes: set[str] | list[str] | frozenset[str], days: int | None) -> tuple[KeyRecord, str]:
    """A new key; returns the record and the value, which is never stored and cannot be shown again."""
    n = clean_name(name)
    sc = normalize_scopes(scopes)
    if not sc:
        raise ValueError("choose at least one permission")
    if days not in EXPIRY_DAYS:
        raise ValueError(f"expiry must be one of {EXPIRY_DAYS}")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    created = _now()
    expires = (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds") if days else None
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO api_keys(name, prefix, sha256, scopes, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?)",
            (n, token[:10], _digest(token), " ".join(sorted(sc)), created, expires),
        )
        rec = _row(conn.execute("SELECT * FROM api_keys WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()
    return rec, token


def list_keys() -> list[KeyRecord]:
    conn = _connect()
    try:
        return [_row(r) for r in conn.execute("SELECT * FROM api_keys ORDER BY revoked_at IS NOT NULL, id DESC")]
    finally:
        conn.close()


def revoke(key_id: int) -> bool:
    """True when an active key was revoked now; revoking twice is harmless."""
    conn = _connect()
    try:
        cur = conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (_now(), key_id))
        return cur.rowcount == 1
    finally:
        conn.close()


_touched: dict[int, float] = {}


def verify(token: str) -> KeyRecord | None:
    """The active key with this value, or None. Lookup by SHA-256: the value itself is never compared or stored."""
    if not token or not token.strip().startswith(TOKEN_PREFIX):
        return None
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM api_keys WHERE sha256=?", (_digest(token),)).fetchone()
        if r is None:
            return None
        rec = _row(r)
        if not rec.active:
            return None
        now = time.monotonic()
        if now - _touched.get(rec.id, -TOUCH_EVERY_S) >= TOUCH_EVERY_S:
            _touched[rec.id] = now
            conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (_now(), rec.id))
        return rec
    finally:
        conn.close()


def any_active(scope: str) -> bool:
    """An active dashboard key holding ``scope`` exists (decides 503 'no key configured' vs 401)."""
    return any(k.active and scope in k.scopes for k in list_keys())
