"""Compatibility shim for Frappe and ERPNext v15 and v16.

Rather than branching on version numbers all over the codebase, everything
that differs between the two lives here. Where possible the check is a
feature probe rather than a version comparison, because version strings lie
on develop branches and on forks.

Anything that genuinely cannot be probed is gated on FRAPPE_MAJOR.
"""

from __future__ import annotations

import inspect
from functools import cache, lru_cache

import frappe


def _major(app: str) -> int:
    try:
        v = frappe.get_attr(f"{app}.__version__") or "0"
    except Exception:
        try:
            v = frappe.get_installed_apps() and frappe.get_hooks("app_version", app_name=app)[0]
        except Exception:
            v = "0"
    try:
        return int(str(v).split(".")[0])
    except (ValueError, IndexError):
        return 0


FRAPPE_MAJOR = _major("frappe")
ERPNEXT_MAJOR = _major("erpnext")
IS_V16_PLUS = FRAPPE_MAJOR >= 16


# ---------------------------------------------------------------------------
# get_list
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_list_params() -> set[str]:
    try:
        return set(inspect.signature(frappe.get_list).parameters)
    except (TypeError, ValueError):
        return set()


def safe_get_list(doctype: str, **kwargs):
    """frappe.get_list with unsupported kwargs stripped rather than raising.

    v16 tightened a few argument names. ignore_ifnull in particular is
    present in v15 and gone in some v16 builds, and passing it blows up a
    query that would otherwise be fine.
    """
    params = _get_list_params()
    if params:
        kwargs = {k: v for k, v in kwargs.items() if k in params or k in ("filters", "fields")}
    return frappe.get_list(doctype, **kwargs)


# ---------------------------------------------------------------------------
# safe_exec
# ---------------------------------------------------------------------------
def call_safe_exec(code: str, namespace: dict):
    """Returns (locals_dict, stdout). Absorbs the signature drift.

    v15 returns (globals, locals). Later builds return (globals, locals, stdout)
    and accept restrict_commit_rollback and script_filename.
    """
    from frappe.utils.safe_exec import safe_exec

    params = set(inspect.signature(safe_exec).parameters)
    kwargs = {"_globals": namespace, "_locals": namespace}
    if "restrict_commit_rollback" in params:
        kwargs["restrict_commit_rollback"] = True
    if "script_filename" in params:
        kwargs["script_filename"] = "research_agent_analysis"

    out = safe_exec(code, **kwargs)
    if isinstance(out, tuple) and len(out) >= 3:
        return out[1] or {}, out[2] or ""
    if isinstance(out, tuple) and len(out) == 2:
        return out[1] or {}, ""
    return namespace, ""


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
def run_report(report_doc, filters: dict):
    """Report.get_data across versions. Returns (columns, rows)."""
    params = set(inspect.signature(report_doc.get_data).parameters)
    kwargs = {"filters": filters or {}, "as_dict": True}
    if "ignore_prepared_report" in params:
        kwargs["ignore_prepared_report"] = True
    if "are_default_filters" in params:
        kwargs["are_default_filters"] = False
    result = report_doc.get_data(**kwargs)
    if isinstance(result, tuple):
        return ([*list(result), None, None])[:2]
    return result, []


# ---------------------------------------------------------------------------
# ERPNext feature probes
# ---------------------------------------------------------------------------
@cache
def has_doctype(name: str) -> bool:
    return bool(frappe.db.exists("DocType", name))


@cache
def has_field(doctype: str, fieldname: str) -> bool:
    if not has_doctype(doctype):
        return False
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


def pick_field(doctype: str, *candidates: str) -> str | None:
    """First fieldname that actually exists. For fields renamed between versions."""
    for c in candidates:
        if has_field(doctype, c):
            return c
    return None


def default_company(user: str | None = None) -> str | None:
    company = frappe.defaults.get_user_default("Company", user or frappe.session.user)
    if company:
        return company
    companies = frappe.get_list("Company", pluck="name", limit_page_length=1)
    return companies[0] if companies else None


def version_report() -> dict:
    """Shown on the settings form so support tickets start with real numbers."""
    return {
        "frappe": frappe.__version__,
        "erpnext": _safe_version("erpnext"),
        "frappe_major": FRAPPE_MAJOR,
        "erpnext_major": ERPNEXT_MAJOR,
        "manufacturing_installed": has_doctype("Work Order"),
        "pos_installed": has_doctype("POS Invoice"),
        "gross_profit_report": frappe.db.exists("Report", "Gross Profit"),
    }


def _safe_version(app: str) -> str:
    try:
        return frappe.get_attr(f"{app}.__version__")
    except Exception:
        return "unknown"
