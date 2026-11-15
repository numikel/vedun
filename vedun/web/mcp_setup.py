"""Data of the dashboard page ``/connect``: MCP endpoint status, tool list and ready-made client configurations.

Snippets never contain the API key. Each one references an environment variable or lets the client prompt for
the value; only the Claude Desktop bridge needs the key pasted into its own config file, and the page says so.
Formats checked against the client docs on 2026-11-11 (Claude Code, VS Code, Cursor, mcp-remote).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from .i18n import t

KEY_HEADER = "X-API-Key"


@dataclass(frozen=True)
class Snippet:
    """One client configuration shown with a copy button."""

    key: str
    title: str
    where: str  # file or command the snippet goes into
    text: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def client_snippets(url: str, name: str, key_env: str) -> list[Snippet]:
    """Configurations for the common MCP clients, all pointing at ``url`` with the key from ``key_env``."""
    ref = "${" + key_env + "}"
    http_entry = {"type": "http", "url": url, "headers": {KEY_HEADER: ref}}
    return [
        Snippet(
            "claude-code",
            "Claude Code",
            t("terminal (bash or PowerShell), outside a claude session"),
            f"claude mcp add-json --scope user {name} '{json.dumps(http_entry)}'",
            (
                t(
                    "Claude Code substitutes {ref} on every connection, so the key never reaches the "
                    "configuration file. Set the variable {key_env} in the environment you start claude from.",
                    ref=ref,
                    key_env=key_env,
                ),
                t("Instead of --scope user: --scope project writes the entry to the project's .mcp.json (no key)."),
                t(
                    "On Windows use PowerShell 7.3 or newer, or Git Bash: older PowerShell drops the quotes "
                    "inside the JSON passed to a program."
                ),
                t("Check: claude mcp list, or /mcp in a session (a green marker next to the name)."),
            ),
        ),
        Snippet(
            "kprun",
            t("Claude Code through kprun (secrets manager)"),
            t("terminal, outside a claude session"),
            f'claude mcp add --scope user {name} -- kprun mcp -e {name} --header "{KEY_HEADER}: '
            f'{{{{{key_env}}}}}" {url}',
            (
                t(
                    "kprun reads the key from the entry {name} when the bridge starts; it is neither in the "
                    "environment nor in any file.",
                    name=name,
                ),
                t("Optional variant: requires kprun to be installed and an entry holding the key."),
            ),
        ),
        Snippet(
            "cursor",
            "Cursor",
            "~/.cursor/mcp.json (globalnie) albo .cursor/mcp.json (projekt)",
            _json({"mcpServers": {name: {"url": url, "headers": {KEY_HEADER: "${env:" + key_env + "}"}}}}),
            (
                t(
                    "Cursor reads {ref} from the environment variables; restart Cursor after changing them.",
                    ref="${env:" + key_env + "}",
                ),
            ),
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
            (t("VS Code asks for the key when the server first starts and keeps it in its secure storage."),),
        ),
        Snippet(
            "claude-desktop",
            "Claude Desktop (most mcp-remote)",
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
            (
                t(
                    "Claude Desktop reaches a remote server only through connectors, without a custom header, "
                    "hence the local mcp-remote bridge (needs Node.js 18+)."
                ),
                t("The key is stored here in the configuration file as plain text: protect that file."),
                t("A header without a space after the colon avoids the Windows bug with spaces in arguments."),
            ),
        ),
    ]


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
        ctx.update(server_name="vedun", protocol="", version="", key_env="VEDUN_API_KEY", auth_ok=None)
    else:
        key_env = C.ENV_PREFIX + S.SECRET_VARS[0]
        ctx.update(
            server_name=mcp_app.SERVER_NAME,
            protocol=mcp_app.PROTOCOL_REVISION,
            version=S.VERSION,
            key_env=key_env,
            auth_ok=key_env not in S.missing_secrets(),
            tools=[
                {
                    "name": mcp_tools.localize(n),
                    "title": t,
                    "summary": mcp_tools.localize(d.strip().split("\n", 1)[0]),
                    "paid": paid,
                }
                for n, t, d, _fn, paid in mcp_tools.TOOL_SPECS
            ],
        )
    ctx["snippets"] = client_snippets(url, ctx["server_name"], ctx["key_env"])
    return ctx
