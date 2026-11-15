"""Dashboard translations: English source text, Polish catalogue, per-request language.

The language of a page is, in this order: a valid ``?lang=`` query value, a valid ``vedun_lang`` cookie,
``vedun.config.UI_LANGUAGE`` (the corpus profile key ``ui_language``). Unknown values are ignored.
The router dependency :func:`activate` stores the choice in a context variable, so :func:`t`, :func:`plural`
and the number/date formatters of ``web.data`` need no request argument (also inside the sync endpoints, which
run in a worker thread with a copy of the context).

Texts are keyed by their English source (``t("Search failed: {error}", error=...)``); the Polish translations
live in :mod:`vedun.web.catalog_pl`. A text without a Polish entry is shown in English. With format arguments
``t`` returns :class:`~markupsafe.Markup` and escapes every argument, so user input stays safe in HTML.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from markupsafe import Markup

from vedun import config as C

from .catalog_pl import JS_STRINGS, PL, PL_PLURAL

QUERY_PARAM = "lang"
COOKIE_NAME = "vedun_lang"
COOKIE_MAX_AGE = 365 * 24 * 3600
LANG_NAMES = {"en": "English", "pl": "Polski"}  # shown in the language's own name

_current: ContextVar[str | None] = ContextVar("vedun_ui_lang", default=None)


def valid(value: Any) -> str | None:
    """``value`` when it is a supported language code, else None."""
    return value if isinstance(value, str) and value in C.UI_LANGUAGES else None


def current() -> str:
    """Language of the request being served (the profile default outside a request)."""
    return valid(_current.get()) or C.UI_LANGUAGE


def resolve(request: Request) -> str:
    """Query value > cookie > profile default; unknown values are skipped."""
    return valid(request.query_params.get(QUERY_PARAM)) or valid(request.cookies.get(COOKIE_NAME)) or C.UI_LANGUAGE


async def activate(request: Request) -> None:
    """Router dependency (async on purpose: the context variable must be set in the request's own task)."""
    _current.set(resolve(request))


def remember(request: Request, response: Response, lang: str) -> None:
    """Persist an explicit ``?lang=`` choice in the cookie (nothing is written for ordinary page views)."""
    if valid(request.query_params.get(QUERY_PARAM)) and request.cookies.get(COOKIE_NAME) != lang:
        response.set_cookie(COOKIE_NAME, lang, max_age=COOKIE_MAX_AGE, path="/", samesite="lax", httponly=True)


def N_(text: str) -> str:  # noqa: N802 - gettext convention: marks a string for the catalogue, translated later
    """Identity marker for texts that are stored first and translated when shown."""
    return text


def t(text: str, **fmt: Any) -> str:
    """``text`` in the current language; with ``fmt`` the ``{placeholders}`` are filled in (HTML-escaped)."""
    out = PL.get(text, text) if current() == "pl" else text
    return Markup(out).format(**fmt) if fmt else out


def plural(n: Any, one: str, other: str, key: str | None = None) -> str:
    """Noun form for ``n``: English ``one``/``other``; Polish has three forms, looked up by ``key or one``."""
    n = abs(int(n))
    forms = PL_PLURAL.get(key or one) if current() == "pl" else None
    if forms is None:
        return one if n == 1 else other
    if n == 1:
        return forms[0]
    return forms[1] if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else forms[2]


def js_payload(lang: str) -> dict[str, str]:
    """Translations of the strings app.js shows (empty for English: the script carries the source text)."""
    return {s: PL[s] for s in JS_STRINGS} if lang == "pl" else {}
