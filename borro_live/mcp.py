"""A thin client for Borro's MCP server: JSON-RPC 2.0 over HTTP, no session, no key."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MCP_URL = "https://borro.markets/mcp"
PROTOCOL = "2025-03-26"


class McpError(Exception):
    """The server could not be reached or answered with a JSON-RPC error."""


class ToolRefused(McpError):
    """A tool answered in words instead of an answer: an unlisted ticker, too little money, no position."""


def http_post(url: str, payload: dict, timeout: float, what: str) -> dict:
    """POST JSON, return JSON. Errors never carry the address: an RPC address can hold a key."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "borro-live"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise McpError(f"{what} answered HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise McpError(f"{what} could not be reached") from None
    except ValueError:
        raise McpError(f"{what} did not answer in JSON") from None


class McpClient:
    """initialize, tools/list and tools/call against Borro's MCP endpoint.

    ``transport`` replaces the HTTP call (a function taking the JSON-RPC payload and returning the response),
    which is how the tests replay recorded answers.
    """

    def __init__(self, url: str | None = None, timeout: float = 60, transport=None):
        self.url = url or os.environ.get("BORRO_MCP_URL") or MCP_URL
        self.timeout = timeout
        self._transport = transport or (lambda payload: http_post(self.url, payload, self.timeout, "Borro's MCP server"))
        self._id = 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        res = self._transport(payload)
        if not isinstance(res, dict) or "result" not in res:
            err = (res or {}).get("error") if isinstance(res, dict) else None
            raise McpError(f"{method}: {(err or {}).get('message', 'no result')}")
        return res["result"]

    def initialize(self) -> dict:
        return self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "borro-live", "version": "0.1.0"}})

    def tools_list(self) -> list[dict]:
        return self._rpc("tools/list")["tools"]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """One tool call. Returns the tool's structured answer; a refusal in words raises ToolRefused."""
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        text = next((c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"), "")
        if res.get("isError"):
            raise ToolRefused(text or f"{name} was refused")
        if isinstance(res.get("structuredContent"), dict):
            return res["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            raise McpError(f"{name} did not answer in JSON") from None
