"""Data of the dashboard page ``/connect``: MCP endpoint status, tool list and ready-made client configurations.

Snippets never contain the API key. Each one references an environment variable or lets the client prompt for
the value; only the Claude Desktop bridge needs the key pasted into its own config file, and the page says so.
Formats checked against the client docs on 2026-11-11 (Claude Code, VS Code, Cursor, mcp-remote).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from markupsafe import Markup

from .i18n import t

KEY_HEADER = "X-API-Key"
KPRUN_URL = "https://kprun.michalsk.pl"


@dataclass(frozen=True)
class Snippet:
    """One client configuration shown with a copy button."""

    key: str
    title: str
    where: str  # file or command the snippet goes into
    text: str


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def client_snippets(url: str, name: str, key_env: str) -> list[Snippet]:
    """Configurations for the common MCP clients, all pointing at ``url`` with the key from ``key_env``."""
    ref = "${" + key_env + "}"
    http_entry = {"type": "http", "url": url, "headers": {KEY_HEADER: ref}}
    kprun = Markup(f'<a href="{KPRUN_URL}" target="_blank" rel="noopener">kprun</a>')
    return [
        Snippet(
            "claude-code",
            "Claude Code",
            t("terminal (bash or PowerShell), outside a claude session"),
            f"claude mcp add-json --scope user {name} '{json.dumps(http_entry)}'",
        ),
        Snippet(
            "kprun",
            t("Claude Code through kprun (secrets manager)"),
            t("terminal, outside a claude session, requires {kprun}", kprun=kprun),
            f'claude mcp add --scope user {name} -- kprun mcp -e {name} --header "{KEY_HEADER}: '
            f'{{{{{key_env}}}}}" {url}',
        ),
        Snippet(
            "cursor",
            "Cursor",
            t("~/.cursor/mcp.json (global) or .cursor/mcp.json (project)"),
            _json({"mcpServers": {name: {"url": url, "headers": {KEY_HEADER: "${env:" + key_env + "}"}}}}),
        ),
        Snippet(
            "vscode",
            "VS Code (Copilot)",
            t(".vscode/mcp.json or the command “MCP: Open User Configuration”"),
            _json(
                {
                    "servers": {name: {**http_entry, "headers": {KEY_HEADER: f"${{input:{name}-api-key}}"}}},
                    "inputs": [
                        {"type": "promptString", "id": f"{name}-api-key", "description": "API key", "password": True}
                    ],
                }
            ),
        ),
        Snippet(
            "claude-desktop",
            t("Claude Desktop (mcp-remote bridge)"),
            "%APPDATA%\\Claude\\claude_desktop_config.json (Windows), "
            "~/Library/Application Support/Claude/claude_desktop_config.json (macOS)",
            _json(
                {
                    "mcpServers": {
                        name: {
                            "command": "npx",
                            "args": [
                                "-y",
                                "mcp-remote@latest",
                                url,
                                "--transport",
                                "http-only",
                                "--allow-http",
                                "--header",
                                KEY_HEADER + ":${MCP_KEY}",
                            ],
                            "env": {"MCP_KEY": t("<paste the API key>")},
                        }
                    }
                }
            ),
        ),
    ]


_FIRST_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s+|$)(.*)$", re.S)


def _tool_text(description: str) -> tuple[str, str]:
    """(first sentence, the rest) of a tool description: the list shows one line, the rest folds out."""
    text = " ".join(description.split())
    m = _FIRST_SENTENCE.match(text)
    return (m[1], m[2].strip()) if m else (text, "")


def _mcp_mounted(request: Request) -> bool:
    return any(getattr(r, "path", "") in ("/mcp", "/mcp/") for r in request.app.routes)


def context(request: Request) -> dict[str, Any]:
    """Everything ``connect.html`` shows. Degrades to an informative page when the MCP module is absent."""
    url = str(request.base_url).rstrip("/") + "/mcp"
    ctx: dict[str, Any] = {"mcp_url": url, "mounted": _mcp_mounted(request), "tools": [], "key_header": KEY_HEADER}
    try:
        from vedun import config as C
        from vedun.server import mcp_app, mcp_tools
        from vedun.server import settings as S
    except ModuleNotFoundError:
        ctx.update(server_name="vedun", protocol="", version="", key_env="VEDUN_API_KEY")
    else:
        key_env = C.ENV_PREFIX + S.SECRET_VARS[0]
        ctx.update(
            server_name=mcp_app.SERVER_NAME,
            protocol=mcp_app.PROTOCOL_REVISION,
            version=S.VERSION,
            key_env=key_env,
            tools=[
                {
                    "name": mcp_tools.localize(n),
                    "title": title,
                    "summary": summary,
                    "details": details,
                    "paid": paid,
                }
                for n, title, d, _fn, paid in mcp_tools.TOOL_SPECS
                for summary, details in [_tool_text(mcp_tools.localize(d))]
            ],
        )
    ctx["snippets"] = client_snippets(url, ctx["server_name"], ctx["key_env"])
    return ctx
