"""Server settings (environment driven) and the read-only database opener used by the HTTP layer.

Everything configurable lives in ``VEDUN_*`` environment variables (see the README, "Server and Docker"):

* library paths: ``VEDUN_ROOT``, ``VEDUN_DB``, ``VEDUN_MD_DIR``, ``VEDUN_FIG_DIR``, ``VEDUN_REPORT_DIR``,
  ``VEDUN_PDF_DIR``, ``VEDUN_STATE_DIR`` (read in :mod:`vedun.config`);
* server: ``VEDUN_REPO_DIR`` (folder scanned by check-citations; default ``/repo`` in a container, otherwise
  the current folder), ``VEDUN_ALLOWED_HOSTS`` (comma list for the Host-header check),
  ``VEDUN_MAX_UPLOAD_MB`` (per PDF, default 50).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from vedun import config as C

VERSION = "0.1.2"
SHEET_RE = C.doc_id_re().pattern  # corpus profile (config.DOC_ID_PATTERN), read once at import
PDF_NAME_RE = C.pdf_name_re().pattern
REPORT_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}\.(md|json)$"
DEFAULT_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver", "vedun.internal", "host.docker.internal"]


def repo_dir() -> Path:
    """Folder that ``check-citations`` may scan (jobs refuse paths outside it)."""
    env = C.env("REPO_DIR")
    if env:
        return Path(env)
    if Path("/repo").is_dir():
        return Path("/repo")
    return Path.cwd()  # host run without VEDUN_REPO_DIR: the folder the service was started from


def allowed_hosts() -> list[str]:
    env = C.env("ALLOWED_HOSTS")
    return [h.strip() for h in env.split(",") if h.strip()] if env else DEFAULT_ALLOWED_HOSTS


def port() -> int:
    return int(C.env("PORT", "8765"))


def allowed_origin_hosts() -> set[str]:
    """``host:port`` values accepted in an ``Origin`` header of a state-changing request."""
    env = C.env("ALLOWED_ORIGINS")
    if env:
        return {h.strip().lower() for h in env.split(",") if h.strip()}
    return {f"127.0.0.1:{port()}", f"localhost:{port()}"}


def rate_limit_per_min() -> int:
    return int(C.env("RATE_LIMIT_PER_MIN", "60"))


def daily_budget_usd() -> float:
    return float(C.env("DAILY_BUDGET_USD", "3.0"))


# --- credentials (env only, injected by kprun entry ``vedun``; never logged, never echoed)
SECRET_VARS = ("API_KEY", "DASH_USER", "DASH_PASSWORD")


def secret(name: str) -> str:
    """Credential ``name`` (``API_KEY`` ...) from ``VEDUN_<name>``."""
    return C.env(name) or ""


def missing_secrets() -> list[str]:
    """Variable names (not values) of the unset credentials."""
    return [C.ENV_PREFIX + n for n in SECRET_VARS if not secret(n)]


def missing_dashboard_secrets() -> list[str]:
    """Unset dashboard login variables: without them every protected route answers 503 (keys are managed there)."""
    return [C.ENV_PREFIX + n for n in SECRET_VARS[1:] if not secret(n)]


def api_key_var() -> str:
    """Name of the master key variable (``VEDUN_API_KEY``)."""
    return C.ENV_PREFIX + SECRET_VARS[0]


def max_upload_bytes() -> int:
    return int(float(C.env("MAX_UPLOAD_MB", "50")) * 1_000_000)


def incoming_dir() -> Path:
    """Staging folder for uploaded PDFs, inside the PDF folder (``plan_sync`` only globs ``*.pdf`` at top level)."""
    return C.DEFAULT_PDF_DIR / ".incoming"


def mmap_bytes() -> int:
    """Memory-map size for read connections (``VEDUN_MMAP_MB``, default 1024; 0 turns it off)."""
    return max(0, int(C.env("MMAP_MB", "1024") or 0)) * 1024 * 1024


def ensure_db() -> None:
    """Create an empty knowledge base on the first start: the HTTP layer only reads, so without the file every route,
    /api/health included, would answer 503 until the first sync and a fresh install would never turn healthy."""
    if not C.DB_PATH.exists():
        from vedun.db import connect

        connect().close()


def open_readonly() -> sqlite3.Connection:
    """Read-only connection with sqlite-vec loaded.

    ``vedun.db.connect`` runs ``init_schema`` (an ``INSERT OR REPLACE`` into ``meta``) on every open, which would
    take the write lock on each request and collide with a running ``sync``. The HTTP layer only reads.
    Raises ``sqlite3.OperationalError`` when the database file does not exist.
    """
    uri = C.DB_PATH.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        # sqlite-vec scans every vector per query; through a Docker bind mount plain reads took 25-30 s for 240 MB of
        # vectors, memory-mapped pages stay in the kernel cache across connections (0.2-0.6 s after the first query)
        conn.execute(f"PRAGMA mmap_size={mmap_bytes()}")
        from vedun.db import needs_migration

        if needs_migration(conn):  # a read-only connection cannot migrate; queries would fail one by one
            raise sqlite3.OperationalError(
                "the knowledge base file has an older schema: stop the server and run `python -m vedun migrate`"
            )
    except BaseException:
        conn.close()
        raise
    return conn
