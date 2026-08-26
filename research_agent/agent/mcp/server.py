"""MCP server.

The other direction: this exposes the app's ERPNext tools as an MCP server so
Claude Desktop, Claude Code, Cowork or any other MCP client can query the ERP
directly, with the same permission model.

Endpoint:
    POST https://your-site.com/api/method/research_agent.agent.mcp.server.handle

Authentication reuses Frappe's own API key mechanism, so the connecting client
inherits a real ERPNext user and every permission check in erpnext_data.py
applies unchanged. There is no separate token to leak and no shared service
account.

Client config for Claude Desktop:

    {
      "mcpServers": {
        "erpnext": {
          "url": "https://erp.example.com/api/method/research_agent.agent.mcp.server.handle",
          "headers": { "Authorization": "token <api_key>:<api_secret>" }
        }
      }
    }
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from research_agent.agent.registry import available_tools, execute

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "erpnext-research-agent", "version": "0.1.0"}


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


@frappe.whitelist(allow_guest=False, methods=["POST"])
def handle():
    """Single JSON-RPC endpoint implementing the MCP tool surface."""
    settings = frappe.get_cached_doc("Research Agent Settings")
    if not settings.enable_mcp_server:
        frappe.throw(_("The MCP server is switched off in Research Agent Settings."), frappe.PermissionError)

    try:
        body = frappe.request.get_json(force=True) or {}
    except Exception:
        return _err(None, -32700, "Parse error")

    rid = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        return _ok(
            rid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "ERPNext business data. Start with erp_company_context, then "
                    "erp_list_doctypes and erp_describe_doctype to find real fieldnames "
                    "before filtering. Every call runs under the authenticated ERPNext "
                    "user's permissions."
                ),
            },
        )

    if method in ("notifications/initialized", "ping"):
        return _ok(rid, {})

    if method == "tools/list":
        # MCP clients render their own UI, so visualisation tools are pointless
        # over this transport. Expose data and analysis only.
        tools = available_tools(include_mcp=False)
        exposed = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.parameters,
                "annotations": {"readOnlyHint": not t.writes, "title": t.name.replace("_", " ").title()},
            }
            for t in tools.values()
            if t.category in ("ERPNext", "Analysis", "Web Research")
        ]
        return _ok(rid, {"tools": exposed})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        result = execute(name, args)
        if not result.get("ok"):
            return _ok(
                rid,
                {"content": [{"type": "text", "text": result.get("error", "Tool failed")}], "isError": True},
            )
        payload = json.dumps(result.get("data"), default=str, indent=2)
        return _ok(
            rid,
            {
                "content": [{"type": "text", "text": payload[:200000]}],
                "structuredContent": result.get("data") if isinstance(result.get("data"), dict) else None,
                "isError": False,
            },
        )

    return _err(rid, -32601, f"Method not found: {method}")
