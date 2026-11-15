"""Static scan of the dashboard sources for translatable texts (used by ``test_web_i18n``).

Collects every English source string passed to ``_()`` / ``t()`` / ``N_()`` / ``plural`` and ``T()`` in the
JavaScript, so a test can prove that the Polish catalogue is complete and has no stale entries.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from jinja2 import Environment, nodes

WEB = Path(__file__).resolve().parents[1] / "vedun" / "web"
TEMPLATES = WEB / "templates"
PY_SOURCES = ("routes.py", "data.py", "mcp_setup.py", "reports.py")
_JS_CALL = re.compile(r'\bT\(\s*"((?:[^"\\]|\\.)*)"')


def template_texts() -> tuple[set[str], set[str]]:
    """``(texts, plural keys)`` found in the templates; dynamic calls like ``_(label)`` are skipped."""
    env = Environment()  # noqa: S701 - only parsed, never rendered
    texts: set[str] = set()
    plurals: set[str] = set()
    for path in sorted(TEMPLATES.rglob("*.html")):
        tree = env.parse(path.read_text(encoding="utf-8"))
        for call in tree.find_all(nodes.Call):
            if isinstance(call.node, nodes.Name) and call.node.name == "_" and call.args:
                first = call.args[0]
                if isinstance(first, nodes.Const) and isinstance(first.value, str):
                    texts.add(first.value)
        for flt in tree.find_all(nodes.Filter):
            if flt.name == "plural" and flt.args and isinstance(flt.args[0], nodes.Const):
                key = flt.args[0].value
                for kw in flt.kwargs:
                    if kw.key == "key" and isinstance(kw.value, nodes.Const):
                        key = kw.value.value
                plurals.add(key)
    return texts, plurals


def python_texts() -> set[str]:
    """First-argument literals of ``t(...)`` and ``N_(...)`` in the web package."""
    out: set[str] = set()
    for name in PY_SOURCES:
        for node in ast.walk(ast.parse((WEB / name).read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"t", "N_"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                out.add(node.args[0].value)
    return out


def js_texts() -> set[str]:
    src = (WEB / "static" / "js" / "app.js").read_text(encoding="utf-8")
    return {m.replace('\\"', '"') for m in _JS_CALL.findall(src)}
