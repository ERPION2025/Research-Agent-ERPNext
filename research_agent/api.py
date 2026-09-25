"""Whitelisted endpoints for the desk page.

Everything here runs as the logged in user. Sessions are owned by the user
who started them, and the Research Session doctype has an owner-only read
rule, so one user cannot open another user's research even if they guess the
name.
"""

from __future__ import annotations

import json

import frappe
from frappe import _


@frappe.whitelist()
def start_session(prompt: str, title: str | None = None, background: int = 1) -> dict:
    """Create a Research Session and run it, in a worker by default."""
    prompt = (prompt or "").strip()
    if not prompt:
        frappe.throw(_("Ask a question first."))
    if len(prompt) > 4000:
        frappe.throw(_("Question is too long. Keep it under 4000 characters."))

    settings = frappe.get_cached_doc("Research Agent Settings")
    if not settings.enabled:
        frappe.throw(_("Research Agent is disabled. Ask your administrator to enable it."))

    _check_daily_quota(settings)

    session = frappe.get_doc(
        {
            "doctype": "Research Session",
            "title": (title or prompt)[:140],
            "prompt": prompt,
            "status": "Queued",
        }
    ).insert()
    frappe.db.commit()

    if frappe.utils.cint(background):
        frappe.enqueue(
            "research_agent.agent.loop.run_session",
            queue="long",
            timeout=1800,
            session_name=session.name,
            enqueue_after_commit=True,
        )
        return {"session": session.name, "status": "Queued"}

    from research_agent.agent.loop import run_session

    return run_session(session.name)


def _check_daily_quota(settings):
    limit = frappe.utils.cint(settings.daily_session_limit)
    if not limit:
        return
    used = frappe.db.count(
        "Research Session",
        {"owner": frappe.session.user, "creation": [">=", frappe.utils.today()]},
    )
    if used >= limit:
        frappe.throw(_("You have used all {0} research runs for today.").format(limit))


@frappe.whitelist()
def get_session(name: str) -> dict:
    """Full session state for polling or reload."""
    doc = frappe.get_doc("Research Session", name)
    doc.check_permission("read")
    return {
        "name": doc.name,
        "title": doc.title,
        "prompt": doc.prompt,
        "status": doc.status,
        "current_trial": doc.current_trial,
        "trials_used": doc.trials_used,
        "final_answer": doc.final_answer,
        "eval_score": doc.eval_score,
        "eval_detail": json.loads(doc.eval_detail) if doc.eval_detail else None,
        "reflections": doc.reflections,
        "duration_seconds": doc.duration_seconds,
        "tokens": {"input": doc.input_tokens, "output": doc.output_tokens},
        "error_log": doc.error_log,
        "steps": [
            {
                "trial": s.trial,
                "step_type": s.step_type,
                "tool_name": s.tool_name,
                "content": s.content,
                "arguments": s.arguments,
                "output": s.output,
                "duration_ms": s.duration_ms,
            }
            for s in doc.steps
        ],
        "artifacts": [json.loads(a.spec) for a in doc.artifacts if a.spec],
        "citations": [
            {"ref": c.ref, "file": c.file_name, "page": c.page_number,
             "section": c.section_heading, "url": c.file_url, "snippet": c.snippet,
             "attached_to": f"{c.source_doctype} {c.source_docname}".strip()
             if c.source_doctype else None, "used": bool(c.used)}
            for c in doc.citations
        ],
        "uncited_markers": doc.uncited_markers,
    }


@frappe.whitelist()
def recent_sessions(limit: int = 20) -> list[dict]:
    return frappe.get_list(
        "Research Session",
        filters={"owner": frappe.session.user},
        fields=["name", "title", "status", "eval_score", "creation"],
        order_by="creation desc",
        limit_page_length=frappe.utils.cint(limit),
    )


@frappe.whitelist()
def cancel_session(name: str) -> dict:
    doc = frappe.get_doc("Research Session", name)
    doc.check_permission("write")
    doc.db_set("status", "Cancelled")
    return {"status": "Cancelled"}


@frappe.whitelist()
def pin_to_workspace(session: str, workspace: str, artifact_id: str) -> dict:
    """Turn a chart artifact into a real ERPNext Dashboard Chart on a workspace."""
    frappe.only_for(["System Manager", "Dashboard Manager"])
    doc = frappe.get_doc("Research Session", session)
    doc.check_permission("read")

    spec = next((json.loads(a.spec) for a in doc.artifacts if a.artifact_id == artifact_id), None)
    if not spec:
        frappe.throw(_("Artifact not found."))
    if spec.get("type") != "chart":
        frappe.throw(_("Only charts can be pinned to a workspace."))

    chart = frappe.get_doc(
        {
            "doctype": "Dashboard Chart",
            "chart_name": spec["title"][:140],
            "chart_type": "Custom",
            "source": "",
            "type": spec.get("chart_type", "bar"),
            "is_public": 0,
            "custom_options": json.dumps({"data": spec["data"], "generated_by": "Research Agent", "session": session}),
        }
    ).insert(ignore_permissions=True)

    ws = frappe.get_doc("Workspace", workspace)
    ws.append("charts", {"chart_name": chart.name, "label": spec["title"][:140]})
    ws.save(ignore_permissions=True)
    return {"dashboard_chart": chart.name, "workspace": workspace}


@frappe.whitelist()
def test_credentials(provider: str) -> dict:
    """Called by the Test buttons on the settings form."""
    frappe.only_for("System Manager")
    settings = frappe.get_cached_doc("Research Agent Settings")

    if provider in ("OpenAI", "Anthropic"):
        from research_agent.agent.llm import get_llm

        llm = get_llm(role="worker", provider=provider)
        resp = llm.chat(messages=[{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=20)
        return {"ok": True, "model": llm.model, "reply": (resp.content or "").strip()[:60]}

    if provider == "Tavily":
        import requests

        key = settings.get_password("tavily_api_key", raise_exception=False)
        if not key:
            frappe.throw(_("No Tavily API key saved."))
        r = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": "ERPNext", "max_results": 1},
            timeout=30,
        )
        r.raise_for_status()
        return {"ok": True, "results": len(r.json().get("results", []))}

    frappe.throw(_("Unknown provider: {0}").format(provider))


@frappe.whitelist()
def pending_actions(limit: int = 20) -> list[dict]:
    """Action requests waiting on the current user, for the approver's queue."""
    role = frappe.db.get_single_value("Research Agent Settings", "approver_role") or "System Manager"
    if role not in frappe.get_roles():
        return []
    return frappe.get_list(
        "Agent Action Request",
        filters={"status": "Pending Approval"},
        fields=["name", "action_type", "target_doctype", "target_docname", "risk_level",
                "estimated_value", "reason", "owner", "creation"],
        order_by="creation desc",
        limit_page_length=frappe.utils.cint(limit),
    )


@frappe.whitelist()
def compatibility() -> dict:
    """Detected Frappe and ERPNext versions plus which modules are present.
    Support tickets should start here."""
    frappe.only_for("System Manager")
    from research_agent.compat import version_report

    report = version_report()
    frappe.db.set_single_value(
        "Research Agent Settings", "version_info", json.dumps(report, indent=2)
    )
    return report


@frappe.whitelist()
def list_tools() -> dict:
    """What the current user can actually call. Useful for debugging permissions."""
    from research_agent.agent.registry import available_tools

    tools = available_tools()
    grouped: dict[str, list] = {}
    for t in tools.values():
        grouped.setdefault(t.category, []).append(
            {"name": t.name, "description": t.description[:200], "source": t.source}
        )
    return {"user": frappe.session.user, "count": len(tools), "tools": grouped}
