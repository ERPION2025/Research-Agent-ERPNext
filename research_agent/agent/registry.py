"""Tool registry.

Two kinds of tools live side by side and look identical to the model:

  builtin - a Python function in this app, decorated with @tool
  mcp     - a tool discovered from an MCP server row, called over JSON-RPC

Both are exposed to the LLM as JSON Schema function definitions. Both are
filtered by the calling user's roles and by the Agent Tool registry rows,
so an administrator can switch any tool off without touching code.
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import frappe

_BUILTIN: dict[str, Tool] = {}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable
    writes: bool = False
    category: str = "General"
    source: str = "builtin"

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def tool(name: str, description: str, parameters: dict, writes: bool = False, category: str = "General"):
    """Register a Python function as an agent tool."""

    def decorator(fn):
        _BUILTIN[name] = Tool(
            name=name,
            description=description.strip(),
            parameters=parameters,
            handler=fn,
            writes=writes,
            category=category,
        )
        return fn

    return decorator


def _load_builtins():
    """Import the tool modules so their decorators run."""
    from research_agent.agent.tools import (  # noqa: F401
        analysis,
        erpnext_data,
        erpnext_ops,
        knowledge,
        research_report,
        tavily_search,
        visualize,
        write_actions,
    )


def builtin_tools() -> dict[str, Tool]:
    if not _BUILTIN:
        _load_builtins()
    return _BUILTIN


def _enabled_registry_rows() -> dict[str, dict]:
    rows = frappe.get_all(
        "Agent Tool",
        fields=["tool_name", "enabled", "allowed_roles", "mcp_server", "tool_type"],
    )
    return {r.tool_name: r for r in rows}


def _user_allowed(row: dict | None) -> bool:
    if not row:
        # Not in the registry yet, e.g. a freshly added builtin before migrate. Allow.
        return True
    if not row.get("enabled"):
        return False
    allowed = (row.get("allowed_roles") or "").strip()
    if not allowed:
        return True
    user_roles = set(frappe.get_roles())
    return bool(user_roles & {r.strip() for r in allowed.split(",") if r.strip()})


def available_tools(include_web: bool = True, include_mcp: bool = True) -> dict[str, Tool]:
    """Everything the current user is allowed to call, keyed by tool name."""
    registry = _enabled_registry_rows()
    out: dict[str, Tool] = {}

    settings = frappe.get_cached_doc("Research Agent Settings")
    for name, t in builtin_tools().items():
        if t.category == "Web Research" and not include_web:
            continue
        if t.category == "Write Actions" and not settings.allow_write_actions:
            continue
        if t.category == "Documents" and not settings.get("enable_knowledge_base"):
            continue
        if _user_allowed(registry.get(name)):
            out[name] = t

    if include_mcp:
        from research_agent.agent.mcp.client import mcp_tools

        for t in mcp_tools():
            if _user_allowed(registry.get(t.name)):
                out[t.name] = t

    return out


def tool_schemas(tools: dict[str, Tool]) -> list[dict]:
    return [t.schema() for t in tools.values()]


def execute(name: str, arguments: dict, context: dict | None = None) -> dict:
    """Run one tool call and always return a serialisable envelope.

    The envelope shape is deliberately boring so the model can rely on it:
        {"ok": bool, "data": ..., "error": str, "elapsed_ms": int}
    """
    tools = available_tools()
    t = tools.get(name)
    started = time.time()

    if not t:
        return {
            "ok": False,
            "error": f"Tool '{name}' is not available to you. Available: {', '.join(sorted(tools))}",
            "elapsed_ms": 0,
        }

    # Nothing mutates the ERP directly. Write intent goes through the
    # Agent Action Request queue in tools/write_actions.py, which is why no
    # tool is ever registered with writes=True.
    if t.writes:
        return {
            "ok": False,
            "error": (
                "Direct writes are not available. Use erp_propose_create, "
                "erp_propose_update, erp_propose_submit or erp_propose_cancel, "
                "which queue the change for approval."
            ),
            "elapsed_ms": 0,
        }

    try:
        sig = inspect.signature(t.handler)
        kwargs = dict(arguments or {})
        if "context" in sig.parameters:
            kwargs["context"] = context or {}
        # Drop anything the handler does not accept rather than blowing up
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
        data = t.handler(**accepted)
        return {"ok": True, "data": data, "elapsed_ms": int((time.time() - started) * 1000)}
    except frappe.PermissionError as e:
        return {"ok": False, "error": f"Permission denied: {e}", "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as e:
        frappe.log_error(title=f"Research Agent tool failed: {name}", message=frappe.get_traceback())
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_ms": int((time.time() - started) * 1000)}


def envelope_to_text(result: dict, max_chars: int = 12000) -> str:
    """Flatten a tool result for the model, truncating politely."""
    if not result.get("ok"):
        return json.dumps({"error": result.get("error")})
    body = json.dumps(result.get("data"), default=str)
    if len(body) > max_chars:
        body = body[:max_chars] + f'... [truncated, {len(body)} chars total. Narrow your filters or aggregate.]'
    return body


def sync_registry() -> int:
    """Create an Agent Tool row for every builtin. Called on install and migrate."""
    count = 0
    for name, t in builtin_tools().items():
        if frappe.db.exists("Agent Tool", name):
            doc = frappe.get_doc("Agent Tool", name)
            doc.description = t.description
            doc.category = t.category
            doc.save(ignore_permissions=True)
        else:
            frappe.get_doc(
                {
                    "doctype": "Agent Tool",
                    "tool_name": name,
                    "tool_type": "Builtin",
                    "description": t.description,
                    "category": t.category,
                    "is_builtin": 1,
                    "enabled": 1,
                }
            ).insert(ignore_permissions=True)
            count += 1
    frappe.db.commit()
    return count
