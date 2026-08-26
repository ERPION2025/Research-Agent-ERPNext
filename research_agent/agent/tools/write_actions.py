"""The write path.

The agent never writes to ERPNext directly. Not once, not for low-risk
documents, not when a setting says it may. Every mutation goes through the
same three steps:

    propose  ->  Agent Action Request (Draft, Pending Approval)
    approve  ->  a human with the approver role, or an auto-approve rule
    execute  ->  runs as the requesting user, normal permission checks

The reason is not paranoia about the model. It is that an ERP write has a
document trail, and "who created this Sales Order" needs a real answer during
an audit. The Agent Action Request row is that answer: it holds the prompt,
the session, the payload, the approver and the resulting document name.

Auto-approve exists so this is usable day to day. It is a narrow allowlist of
DocTypes plus a value ceiling, and it still writes the request row. The
difference is only whether a human clicks Approve. Submit and Cancel are
never auto-approved, because those move stock and money.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import flt

from research_agent.agent.registry import tool

# Never writable through the agent, no matter what the settings say.
# These are the doctypes that grant power rather than record business events.
HARD_BLOCKED = {
    "User", "Role", "Role Profile", "User Permission", "Custom Role",
    "DocType", "Custom Field", "Property Setter", "Client Script",
    "Server Script", "Scheduled Job Type", "System Settings",
    "Website Settings", "Social Login Key", "OAuth Client", "Webhook",
    "Access Log", "Activity Log", "Error Log", "Deleted Document",
    "Research Agent Settings", "Agent Action Request", "Agent Tool", "MCP Server",
}

VALUE_FIELDS = ("grand_total", "base_grand_total", "total", "amount", "paid_amount", "outstanding_amount")


def _settings():
    return frappe.get_cached_doc("Research Agent Settings")


def _guard(doctype: str, ptype: str):
    s = _settings()
    if not s.allow_write_actions:
        frappe.throw(_("Write actions are switched off in Research Agent Settings."), frappe.PermissionError)
    if doctype in HARD_BLOCKED:
        frappe.throw(_("'{0}' can never be written by the agent.").format(doctype), frappe.PermissionError)

    blocked = {d.strip() for d in (s.write_blocklist or "").split(",") if d.strip()}
    if doctype in blocked:
        frappe.throw(_("'{0}' is on the write blocklist.").format(doctype), frappe.PermissionError)

    allowed = {r.document_type for r in (s.writable_doctypes or []) if r.document_type}
    if allowed and doctype not in allowed:
        frappe.throw(
            _("'{0}' is not in the writable DocType list. Add it in Research Agent Settings.").format(doctype),
            frappe.PermissionError,
        )

    # the requesting user must have the permission themselves
    if not frappe.has_permission(doctype, ptype=ptype):
        frappe.throw(
            _("You do not have {0} permission on {1}, so the agent cannot request it either.").format(ptype, doctype),
            frappe.PermissionError,
        )


def _estimate_value(doctype: str, values: dict) -> float:
    for f in VALUE_FIELDS:
        if values.get(f):
            return flt(values[f])
    # sum the child rows if the header has no total yet
    total = 0.0
    for v in values.values():
        if isinstance(v, list):
            for row in v:
                if isinstance(row, dict):
                    total += flt(row.get("amount") or (flt(row.get("qty")) * flt(row.get("rate"))))
    return total


def _auto_approve(doctype: str, action: str, value: float) -> tuple[bool, str]:
    s = _settings()
    if not s.enable_auto_approve:
        return False, "Auto-approve is off."
    if action in ("Submit", "Cancel", "Delete"):
        return False, "Submit, Cancel and Delete always need a human."

    allowed = {r.document_type for r in (s.auto_approve_doctypes or []) if r.document_type}
    if doctype not in allowed:
        return False, f"{doctype} is not on the auto-approve list."

    ceiling = flt(s.auto_approve_value_limit)
    if ceiling and value > ceiling:
        return False, f"Value {value:,.2f} is over the auto-approve ceiling of {ceiling:,.2f}."

    return True, f"{doctype} is auto-approved under {ceiling:,.2f}." if ceiling else f"{doctype} is auto-approved."


def _create_request(action: str, doctype: str, docname: str | None, payload: dict,
                    reason: str, context: dict | None) -> dict:
    session = (context or {}).get("session")
    value = _estimate_value(doctype, payload if action == "Create" else payload.get("changes", {}))
    auto, why = _auto_approve(doctype, action, value)

    req = frappe.get_doc(
        {
            "doctype": "Agent Action Request",
            "action_type": action,
            "target_doctype": doctype,
            "target_docname": docname,
            "payload": json.dumps(payload, default=str, indent=2),
            "reason": reason,
            "estimated_value": value,
            "risk_level": _risk(action, doctype, value),
            "research_session": session.name if session else None,
            "originating_prompt": (session.prompt if session else "")[:1000],
            "status": "Pending Approval",
            "auto_approve_note": why,
        }
    ).insert()
    frappe.db.commit()

    result = {
        "request": req.name,
        "status": req.status,
        "action": action,
        "target": f"{doctype} {docname or '(new)'}",
        "estimated_value": value,
        "auto_approve": auto,
        "auto_approve_note": why,
    }

    if auto:
        executed = execute_request(req.name, approver=frappe.session.user, is_auto=True)
        result.update({"status": executed["status"], "created_document": executed.get("document")})
    else:
        _notify_approvers(req)
        result["message"] = (
            f"Queued for approval as {req.name}. Nothing has changed in the ERP yet. "
            f"Tell the user what will happen and that it needs approval."
        )

    return result


def _risk(action: str, doctype: str, value: float) -> str:
    if action in ("Cancel", "Delete"):
        return "High"
    if action == "Submit":
        return "High" if value > 0 else "Medium"
    if value > 100000:
        return "High"
    if value > 0:
        return "Medium"
    return "Low"


def _notify_approvers(req):
    role = _settings().approver_role or "System Manager"
    users = frappe.get_all(
        "Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent", limit_page_length=25
    )
    if not users:
        return
    frappe.get_doc(
        {
            "doctype": "Notification Log",
            "subject": _("Agent action needs approval: {0} {1}").format(req.action_type, req.target_doctype),
            "for_user": users[0],
            "type": "Alert",
            "document_type": "Agent Action Request",
            "document_name": req.name,
        }
    ).insert(ignore_permissions=True)
    for u in users[1:]:
        frappe.publish_realtime(
            "agent_action_pending",
            {"request": req.name, "doctype": req.target_doctype, "action": req.action_type},
            user=u,
        )


# ---------------------------------------------------------------------------
# tools the agent can call
# ---------------------------------------------------------------------------
@tool(
    name="erp_propose_create",
    category="Write Actions",
    description="""
    Propose creating a new ERPNext document. This does NOT create anything. It
    queues an Agent Action Request that a human approves, unless the DocType is
    on the auto-approve list.

    Use for things like logging a Lead from market research, raising a Material
    Request when stock is short, or creating a Task for a follow-up you
    identified.

    Pass values exactly as the DocType expects, including child tables as lists
    of dicts. Call erp_describe_doctype first to get real fieldnames. Always
    write a reason that explains why, in one sentence, because that reason is
    what the approver reads.

    After calling this, tell the user plainly that it is pending approval and
    what will happen when approved. Never claim the record exists.
    """,
    parameters={
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "values": {"type": "object", "description": "Field values. Child tables as lists of objects."},
            "reason": {"type": "string", "description": "One sentence the approver will read"},
        },
        "required": ["doctype", "values", "reason"],
    },
)
def erp_propose_create(doctype: str, values: dict, reason: str, context: dict | None = None):
    _guard(doctype, "create")
    values = dict(values or {})
    values.pop("doctype", None)
    return _create_request("Create", doctype, None, values, reason, context)


@tool(
    name="erp_propose_update",
    category="Write Actions",
    description="""
    Propose changing fields on an existing document. Queues an approval request;
    changes nothing directly.

    Only pass the fields you want changed, not the whole document. The approver
    sees a before-and-after diff, so keep the change set small and explain it.

    Cannot be used on submitted documents except for fields ERPNext itself marks
    as allow_on_submit.
    """,
    parameters={
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "name": {"type": "string", "description": "The document name, e.g. 'SO-2026-00042'"},
            "changes": {"type": "object", "description": "Only the fields to change"},
            "reason": {"type": "string"},
        },
        "required": ["doctype", "name", "changes", "reason"],
    },
)
def erp_propose_update(doctype: str, name: str, changes: dict, reason: str, context: dict | None = None):
    _guard(doctype, "write")
    if not frappe.db.exists(doctype, name):
        frappe.throw(_("{0} {1} does not exist.").format(doctype, name))

    doc = frappe.get_doc(doctype, name)
    doc.check_permission("write")

    meta = frappe.get_meta(doctype)
    diff = {}
    for field, new in (changes or {}).items():
        df = meta.get_field(field)
        if not df:
            frappe.throw(_("'{0}' is not a field on {1}.").format(field, doctype))
        if doc.docstatus == 1 and not df.allow_on_submit:
            frappe.throw(_("'{0}' cannot be changed after submission.").format(field))
        diff[field] = {"from": doc.get(field), "to": new}

    return _create_request(
        "Update", doctype, name, {"changes": changes, "diff": diff}, reason, context
    )


@tool(
    name="erp_propose_submit",
    category="Write Actions",
    description="""
    Propose submitting a draft document. Always needs a human approval; this is
    never auto-approved, because submission posts stock and general ledger
    entries.

    Say clearly in the reason what the financial effect will be.
    """,
    parameters={
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "name": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["doctype", "name", "reason"],
    },
)
def erp_propose_submit(doctype: str, name: str, reason: str, context: dict | None = None):
    _guard(doctype, "submit")
    doc = frappe.get_doc(doctype, name)
    doc.check_permission("submit")
    if doc.docstatus != 0:
        frappe.throw(_("{0} {1} is not a draft.").format(doctype, name))
    return _create_request("Submit", doctype, name, {}, reason, context)


@tool(
    name="erp_propose_cancel",
    category="Write Actions",
    description="""
    Propose cancelling a submitted document. Always needs human approval.
    Cancellation reverses ledger entries, so state exactly what will reverse.
    """,
    parameters={
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "name": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["doctype", "name", "reason"],
    },
)
def erp_propose_cancel(doctype: str, name: str, reason: str, context: dict | None = None):
    _guard(doctype, "cancel")
    doc = frappe.get_doc(doctype, name)
    doc.check_permission("cancel")
    if doc.docstatus != 1:
        frappe.throw(_("{0} {1} is not submitted.").format(doctype, name))
    return _create_request("Cancel", doctype, name, {}, reason, context)


@tool(
    name="erp_list_pending_actions",
    category="Write Actions",
    description="List the agent action requests you have raised that are still waiting for approval.",
    parameters={"type": "object", "properties": {}},
)
def erp_list_pending_actions():
    rows = frappe.get_list(
        "Agent Action Request",
        filters={"owner": frappe.session.user, "status": "Pending Approval"},
        fields=["name", "action_type", "target_doctype", "target_docname", "estimated_value", "creation"],
        order_by="creation desc",
        limit_page_length=25,
    )
    return {"pending": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# execution, called from the Agent Action Request form or auto-approve
# ---------------------------------------------------------------------------
def execute_request(name: str, approver: str | None = None, is_auto: bool = False) -> dict:
    """Run an approved request as the user who asked for it.

    Running as the requester rather than the approver is deliberate. The
    approver is saying "yes, do what they asked", not lending their own
    permissions. If the requester cannot create a Sales Order, approval does
    not change that.
    """
    req = frappe.get_doc("Agent Action Request", name)
    if req.status not in ("Pending Approval", "Approved"):
        frappe.throw(_("Request {0} is {1}, nothing to execute.").format(name, req.status))

    payload = json.loads(req.payload or "{}")
    original_user = frappe.session.user

    try:
        frappe.set_user(req.owner)

        if req.action_type == "Create":
            doc = frappe.get_doc({"doctype": req.target_doctype, **payload})
            doc.insert()
            result_name = doc.name

        elif req.action_type == "Update":
            doc = frappe.get_doc(req.target_doctype, req.target_docname)
            for field, value in (payload.get("changes") or {}).items():
                doc.set(field, value)
            doc.save()
            result_name = doc.name

        elif req.action_type == "Submit":
            doc = frappe.get_doc(req.target_doctype, req.target_docname)
            doc.submit()
            result_name = doc.name

        elif req.action_type == "Cancel":
            doc = frappe.get_doc(req.target_doctype, req.target_docname)
            doc.cancel()
            result_name = doc.name

        else:
            frappe.throw(_("Unknown action type: {0}").format(req.action_type))

    except Exception as e:
        frappe.db.rollback()
        frappe.set_user(original_user)
        req.reload()
        req.db_set("status", "Failed")
        req.db_set("error_log", frappe.get_traceback()[:100000])
        frappe.db.commit()
        return {"status": "Failed", "error": str(e), "request": name}

    finally:
        frappe.set_user(original_user)

    req.reload()
    req.db_set("status", "Executed")
    req.db_set("result_docname", result_name)
    req.db_set("approved_by", approver or frappe.session.user)
    req.db_set("approved_on", frappe.utils.now())
    req.db_set("approval_mode", "Auto" if is_auto else "Manual")
    frappe.db.commit()

    return {
        "status": "Executed",
        "request": name,
        "document": f"{req.target_doctype} {result_name}",
        "docname": result_name,
    }
