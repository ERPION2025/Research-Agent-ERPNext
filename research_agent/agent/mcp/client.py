"""MCP client.

Lets the agent borrow tools from any MCP server the administrator registers:
a Postgres warehouse, a Slack workspace, a Google Drive connector, a
Playwright browser, an internal pricing service.

Transport is streamable HTTP (JSON-RPC 2.0 over POST). Stdio is deliberately
not supported, because a Frappe worker running a long-lived subprocess per
request is a bad idea on a shared bench. If you need a stdio server, front it
with mcp-proxy and register the HTTP URL.

Discovery is cached on the MCP Server doctype and refreshed hourly by the
scheduler, so building the tool list for a session costs one database read
rather than a network round trip per server.
"""

from __future__ import annotations

import json

import frappe
import requests
from frappe import _

from research_agent.agent.registry import Tool

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, server_doc):
        self.doc = server_doc
        self.url = server_doc.url
        self._id = 0

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        token = self.doc.get_password("auth_token", raise_exception=False)
        if token:
            scheme = self.doc.auth_scheme or "Bearer"
            headers["Authorization"] = f"{scheme} {token}".strip()
        for row in self.doc.get("custom_headers") or []:
            if row.header_key:
                headers[row.header_key] = row.get_password("header_value", raise_exception=False) or ""
        return headers

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params

        resp = requests.post(
            self.url, json=body, headers=self._headers(), timeout=int(self.doc.timeout or 60)
        )
        resp.raise_for_status()

        payload = self._parse(resp)
        if "error" in payload:
            err = payload["error"]
            raise MCPError(f"{err.get('code')}: {err.get('message')}")
        return payload.get("result", {})

    @staticmethod
    def _parse(resp) -> dict:
        """Handle both plain JSON and SSE-framed responses."""
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" not in ctype:
            return resp.json()
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    obj = json.loads(chunk)
                    if "result" in obj or "error" in obj:
                        return obj
        raise MCPError("No JSON-RPC payload found in the event stream.")

    def initialize(self) -> dict:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "erpnext-research-agent", "version": "0.1.0"},
            },
        )

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "resource":
                parts.append(json.dumps(block.get("resource"), default=str))
            else:
                parts.append(json.dumps(block, default=str))
        return {
            "isError": bool(result.get("isError")),
            "text": "\n".join(parts),
            "structured": result.get("structuredContent"),
        }


# ---------------------------------------------------------------------------
# registry integration
# ---------------------------------------------------------------------------
def _make_handler(server_name: str, remote_name: str):
    def handler(**kwargs):
        doc = frappe.get_doc("MCP Server", server_name)
        out = MCPClient(doc).call_tool(remote_name, kwargs)
        if out["isError"]:
            frappe.throw(_("MCP tool '{0}' failed: {1}").format(remote_name, out["text"][:500]))
        return out.get("structured") or out["text"]

    return handler


def mcp_tools() -> list[Tool]:
    """Every cached tool from every enabled MCP server, namespaced by prefix."""
    out: list[Tool] = []
    servers = frappe.get_all(
        "MCP Server",
        filters={"enabled": 1},
        fields=["name", "tool_prefix", "cached_tools"],
    )
    for s in servers:
        try:
            cached = json.loads(s.cached_tools or "[]")
        except json.JSONDecodeError:
            continue
        prefix = (s.tool_prefix or frappe.scrub(s.name)).strip("_")
        for t in cached:
            local = f"{prefix}__{t['name']}"
            out.append(
                Tool(
                    name=local,
                    description=f"[via {s.name}] {t.get('description') or ''}".strip(),
                    parameters=t.get("inputSchema") or {"type": "object", "properties": {}},
                    handler=_make_handler(s.name, t["name"]),
                    category="MCP",
                    source=f"mcp:{s.name}",
                )
            )
    return out


@frappe.whitelist()
def refresh_server(server: str) -> dict:
    """Re-discover tools on one server. Called from the MCP Server form."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc("MCP Server", server)
    client = MCPClient(doc)
    info = client.initialize()
    tools = client.list_tools()

    doc.db_set("cached_tools", json.dumps(tools, indent=2), update_modified=False)
    doc.db_set("server_info", json.dumps(info.get("serverInfo") or {}, indent=2), update_modified=False)
    doc.db_set("tool_count", len(tools), update_modified=False)
    doc.db_set("last_synced", frappe.utils.now(), update_modified=False)
    doc.db_set("last_error", "", update_modified=False)

    # keep the Agent Tool registry in step so admins can toggle individual tools
    prefix = (doc.tool_prefix or frappe.scrub(doc.name)).strip("_")
    for t in tools:
        local = f"{prefix}__{t['name']}"
        if not frappe.db.exists("Agent Tool", local):
            frappe.get_doc(
                {
                    "doctype": "Agent Tool",
                    "tool_name": local,
                    "tool_type": "MCP",
                    "mcp_server": doc.name,
                    "category": "MCP",
                    "description": (t.get("description") or "")[:500],
                    "enabled": 1,
                }
            ).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"tools": len(tools), "server": info.get("serverInfo")}


def refresh_all_servers():
    """Hourly scheduler job."""
    for name in frappe.get_all("MCP Server", filters={"enabled": 1}, pluck="name"):
        try:
            refresh_server(name)
        except Exception as e:
            frappe.db.set_value("MCP Server", name, "last_error", str(e)[:500], update_modified=False)
            frappe.log_error(title=f"MCP refresh failed: {name}", message=frappe.get_traceback())
    frappe.db.commit()
