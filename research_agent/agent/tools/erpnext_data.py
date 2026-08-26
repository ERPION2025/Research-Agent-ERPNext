"""ERPNext data tools.

Every tool here runs as the logged in user. There is no service account and
no permission bypass anywhere in this module. That is the whole point: a
Sales User asking about margins sees exactly the rows a Sales User can open
in the desk, and a Territory-restricted user gets territory-restricted
answers without the agent knowing anything about it.

Three layers of enforcement:
  1. frappe.get_list applies permissions, user permissions and share rules.
  2. erp_read_query runs raw SELECT, but a hand-written query cannot have
     row-level User Permission conditions verified against it the way
     frappe.get_list can, so it checks doctype-level permission on every
     table referenced and is refused outright to any user who has a User
     Permission restriction at all. Restricted users get erp_fetch_records
     and erp_run_report, which enforce the restriction correctly.
  3. Research Agent Settings holds a DocType allowlist that caps the whole
     surface, so you can ship the agent to a team and expose only Sales
     Invoice, Sales Order and Item if that is all they should see.
"""

from __future__ import annotations

import re

import frappe
from frappe import _

from research_agent.agent.registry import tool
from research_agent.compat import run_report, safe_get_list

MAX_ROWS_HARD_CAP = 5000
BLOCKED_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|replace|"
    r"rename|lock|call|handler|load\s+data|into\s+outfile|into\s+dumpfile|"
    r"information_schema|mysql\.|performance_schema|sleep|benchmark)\b",
    re.IGNORECASE,
)
TABLE_RE = re.compile(r"`?tab([A-Za-z0-9 _\-\.]+?)`?(?=[\s,`\)]|$)")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _settings():
    return frappe.get_cached_doc("Research Agent Settings")


def _allowlist() -> set[str] | None:
    s = _settings()
    names = {r.document_type for r in (s.allowed_doctypes or []) if r.document_type}
    return names or None


def _check_doctype(doctype: str, ptype: str = "read"):
    allow = _allowlist()
    if allow and doctype not in allow:
        frappe.throw(
            _("DocType '{0}' is not in the Research Agent allowlist.").format(doctype),
            frappe.PermissionError,
        )
    if not frappe.has_permission(doctype, ptype=ptype):
        frappe.throw(
            _("You do not have {0} permission on {1}.").format(ptype, doctype),
            frappe.PermissionError,
        )


def _row_cap(requested: int | None) -> int:
    ceiling = min(int(_settings().max_rows_per_query or 500), MAX_ROWS_HARD_CAP)
    return min(int(requested or ceiling), ceiling)


# --------------------------------------------------------------------------
# schema discovery
# --------------------------------------------------------------------------
@tool(
    name="erp_list_doctypes",
    category="ERPNext",
    description="""
    List the ERPNext DocTypes (tables) this user can read, optionally filtered by a
    keyword or module. Always call this first when you are unsure which table holds
    the data. Returns doctype name, module and whether it is a submittable document.
    """,
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Substring to match on the DocType name, e.g. 'invoice'"},
            "module": {"type": "string", "description": "Restrict to one module, e.g. 'Accounts', 'Stock', 'Selling'"},
            "limit": {"type": "integer", "description": "Max results, default 40"},
        },
    },
)
def erp_list_doctypes(keyword: str | None = None, module: str | None = None, limit: int = 40):
    filters = {"istable": 0, "issingle": 0}
    if module:
        filters["module"] = module
    or_filters = {"name": ["like", f"%{keyword}%"]} if keyword else None

    rows = frappe.get_all(
        "DocType",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "module", "is_submittable"],
        limit_page_length=min(int(limit or 40), 200),
        order_by="name asc",
    )
    allow = _allowlist()
    out = []
    for r in rows:
        if allow and r.name not in allow:
            continue
        if not frappe.has_permission(r.name, ptype="read"):
            continue
        out.append(r)
    return {"doctypes": out, "allowlist_active": bool(allow)}


@tool(
    name="erp_describe_doctype",
    category="ERPNext",
    description="""
    Describe one DocType: its fields, data types, link targets and which fields are
    numeric so you know what can be summed or averaged. Call this before writing any
    filter or aggregation so you use real fieldnames rather than guessing.
    """,
    parameters={
        "type": "object",
        "properties": {
            "doctype": {"type": "string", "description": "Exact DocType name, e.g. 'Sales Invoice'"},
        },
        "required": ["doctype"],
    },
)
def erp_describe_doctype(doctype: str):
    _check_doctype(doctype)
    meta = frappe.get_meta(doctype)
    numeric = {"Currency", "Float", "Int", "Percent"}
    fields = []
    for df in meta.fields:
        if df.fieldtype in ("Section Break", "Column Break", "Tab Break", "HTML", "Button"):
            continue
        fields.append(
            {
                "fieldname": df.fieldname,
                "label": df.label,
                "fieldtype": df.fieldtype,
                "options": df.options if df.fieldtype in ("Link", "Select", "Table") else None,
                "numeric": df.fieldtype in numeric,
            }
        )
    return {
        "doctype": doctype,
        "table": f"tab{doctype}",
        "is_submittable": bool(meta.is_submittable),
        "title_field": meta.title_field,
        "standard_fields": ["name", "owner", "creation", "modified", "docstatus"],
        "fields": fields,
        "child_tables": [
            {"fieldname": df.fieldname, "doctype": df.options}
            for df in meta.fields
            if df.fieldtype == "Table"
        ],
    }


# --------------------------------------------------------------------------
# structured reads
# --------------------------------------------------------------------------
@tool(
    name="erp_fetch_records",
    category="ERPNext",
    description="""
    Read records from one DocType with filters, sorting and optional grouping.
    This is the safe default for most questions and fully respects the user's
    permissions and user permissions.

    For aggregation set group_by and aggregate together. Example: revenue by
    customer this year becomes doctype='Sales Invoice',
    filters={"docstatus": 1, "posting_date": [">=", "2026-04-01"]},
    group_by='customer', aggregate='sum', aggregate_field='grand_total'.

    Filter syntax is Frappe's: {"status": "Paid"} for equals, or
    {"posting_date": ["between", ["2026-01-01", "2026-03-31"]]} for operators.
    Supported operators: =, !=, >, <, >=, <=, like, not like, in, not in, between,
    is (with "set" or "not set").
    """,
    parameters={
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fieldnames to return. Ignored when group_by is set.",
            },
            "filters": {"type": "object", "description": "Frappe filter dict"},
            "group_by": {"type": "string", "description": "Fieldname to group by"},
            "aggregate": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
            "aggregate_field": {"type": "string", "description": "Numeric field to aggregate. Not needed for count."},
            "order_by": {"type": "string", "description": "e.g. 'posting_date desc'"},
            "limit": {"type": "integer"},
        },
        "required": ["doctype"],
    },
)
def erp_fetch_records(
    doctype: str,
    fields: list[str] | None = None,
    filters: dict | None = None,
    group_by: str | None = None,
    aggregate: str | None = None,
    aggregate_field: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
):
    _check_doctype(doctype)
    limit = _row_cap(limit)
    meta = frappe.get_meta(doctype)
    valid = {df.fieldname for df in meta.fields} | {
        "name",
        "owner",
        "creation",
        "modified",
        "docstatus",
        "idx",
    }

    def _assert_field(f: str, label: str):
        base = f.split(".")[0].strip("`")
        if base not in valid:
            frappe.throw(_("'{0}' is not a field on {1} ({2}).").format(f, doctype, label))

    if group_by:
        _assert_field(group_by, "group_by")
        if aggregate in ("sum", "avg", "min", "max"):
            if not aggregate_field:
                frappe.throw(_("aggregate_field is required for {0}.").format(aggregate))
            _assert_field(aggregate_field, "aggregate_field")
            expr = f"{aggregate}(`{aggregate_field}`) as value"
        else:
            expr = "count(name) as value"
        select = [f"`{group_by}` as label", expr]
        rows = safe_get_list(
            doctype,
            filters=filters or {},
            fields=select,
            group_by=f"`{group_by}`",
            order_by=order_by or "value desc",
            limit_page_length=limit,
            ignore_ifnull=True,
        )
        return {
            "doctype": doctype,
            "shape": "aggregate",
            "group_by": group_by,
            "measure": f"{aggregate or 'count'}({aggregate_field or 'name'})",
            "row_count": len(rows),
            "rows": rows,
        }

    fields = fields or ["name"]
    for f in fields:
        _assert_field(f, "fields")
    rows = frappe.get_list(
        doctype,
        filters=filters or {},
        fields=fields,
        order_by=order_by or "modified desc",
        limit_page_length=limit,
    )
    return {
        "doctype": doctype,
        "shape": "records",
        "row_count": len(rows),
        "truncated": len(rows) >= limit,
        "rows": rows,
    }


def _has_row_level_restrictions(user: str | None = None) -> bool:
    """True when this user has any User Permission record.

    _check_doctype below only proves the user has *some* read access to a
    doctype, the same as any role grants. A User Permission (a Territory or
    Cost Center restriction, for example) narrows that to specific rows, and
    frappe.has_permission(doctype) without a document does not see it. Raw
    SQL cannot generically have that row-level condition injected the way
    frappe.get_list can, because we do not control the joins or aliases the
    model writes. Rather than silently under-enforce, this fails closed: a
    restricted user is pointed at erp_fetch_records and erp_run_report,
    which do apply the restriction correctly.
    """
    return bool(
        frappe.get_all(
            "User Permission", filters={"user": user or frappe.session.user}, limit_page_length=1
        )
    )


@tool(
    name="erp_read_query",
    category="ERPNext",
    description="""
    Run a single read-only SQL SELECT against the ERPNext database for joins and
    window functions that erp_fetch_records cannot express. MariaDB syntax.
    Table names are `tabSales Invoice` style. Only SELECT and WITH are allowed, and
    every table referenced needs the calling user to already have doctype-level read
    permission. Not available to users with a User Permission restriction (a
    Territory or Cost Center scope, for example), because that row-level narrowing
    cannot be verified against hand-written SQL; use erp_fetch_records or
    erp_run_report instead, both of which enforce it. Always add a LIMIT. Prefer
    erp_fetch_records when it is enough.
    """,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A single SELECT statement, no trailing semicolon"},
            "purpose": {"type": "string", "description": "One line on what this answers, stored in the audit log"},
        },
        "required": ["query", "purpose"],
    },
)
def erp_read_query(query: str, purpose: str = ""):
    if not _settings().allow_raw_sql:
        frappe.throw(_("Raw SQL is switched off in Research Agent Settings."), frappe.PermissionError)
    if _has_row_level_restrictions():
        frappe.throw(
            _(
                "Raw SQL is not available to you because your account has a User Permission "
                "restriction, which cannot be enforced against hand-written SQL. Use "
                "erp_fetch_records or erp_run_report instead."
            ),
            frappe.PermissionError,
        )

    q = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    q = re.sub(r"--.*?$|#.*?$", " ", q, flags=re.MULTILINE).strip().rstrip(";")

    if ";" in q:
        frappe.throw(_("Only one statement is allowed."))
    if not re.match(r"^\s*(select|with)\b", q, re.IGNORECASE):
        frappe.throw(_("Only SELECT or WITH queries are allowed."))
    if BLOCKED_SQL.search(q):
        frappe.throw(_("Query contains a blocked keyword."))

    tables = {t.strip() for t in TABLE_RE.findall(q)}
    if not tables:
        frappe.throw(_("No ERPNext tables found. Use `tabSales Invoice` style names."))
    for dt in tables:
        _check_doctype(dt)

    if not re.search(r"\blimit\b", q, re.IGNORECASE):
        q += f" LIMIT {_row_cap(None)}"

    rows = frappe.db.sql(q, as_dict=True)
    return {
        "shape": "sql",
        "purpose": purpose,
        "tables_read": sorted(tables),
        "row_count": len(rows),
        "rows": rows[: _row_cap(None)],
    }


@tool(
    name="erp_run_report",
    category="ERPNext",
    description="""
    Execute a saved ERPNext report (Query Report or Script Report) such as
    'General Ledger', 'Stock Balance', 'Accounts Receivable', 'Gross Profit' or
    'Sales Analytics'. Use this instead of rebuilding standard accounting logic
    yourself, because the reports already handle posting dates, company filters and
    valuation correctly. Call erp_list_reports first to see what exists.
    """,
    parameters={
        "type": "object",
        "properties": {
            "report_name": {"type": "string"},
            "filters": {"type": "object", "description": "Report filters, e.g. {'company': 'Acme', 'from_date': '2026-01-01'}"},
        },
        "required": ["report_name"],
    },
)
def erp_run_report(report_name: str, filters: dict | None = None):
    if not frappe.db.exists("Report", report_name):
        frappe.throw(_("Report '{0}' not found.").format(report_name))
    report = frappe.get_doc("Report", report_name)
    if report.ref_doctype:
        _check_doctype(report.ref_doctype)
    if not frappe.has_permission("Report", ptype="read", doc=report):
        frappe.throw(_("You cannot run this report."), frappe.PermissionError)

    columns, rows = run_report(report, filters or {})
    rows = rows or []
    cap = _row_cap(None)
    return {
        "shape": "report",
        "report": report_name,
        "filters": filters or {},
        "columns": [
            {"label": c.get("label"), "fieldname": c.get("fieldname"), "fieldtype": c.get("fieldtype")}
            if isinstance(c, dict)
            else {"label": str(c)}
            for c in (columns or [])
        ],
        "row_count": len(rows),
        "truncated": len(rows) > cap,
        "rows": rows[:cap],
    }


@tool(
    name="erp_list_reports",
    category="ERPNext",
    description="Search saved ERPNext reports by keyword or by the DocType they are built on.",
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string"},
            "ref_doctype": {"type": "string"},
        },
    },
)
def erp_list_reports(keyword: str | None = None, ref_doctype: str | None = None):
    filters = {"disabled": 0}
    if ref_doctype:
        filters["ref_doctype"] = ref_doctype
    or_filters = {"name": ["like", f"%{keyword}%"]} if keyword else None
    rows = frappe.get_all(
        "Report",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "ref_doctype", "report_type", "module"],
        limit_page_length=60,
        order_by="name asc",
    )
    return {"reports": [r for r in rows if frappe.has_permission(r.ref_doctype or "Report", ptype="read")]}


@tool(
    name="erp_company_context",
    category="ERPNext",
    description="""
    Fetch the basic accounting context you need before any financial answer: the
    companies this user can see, their default currencies, the current and previous
    fiscal year with exact start and end dates, and today's date on the server.
    Call this once at the start of any question that mentions a period like
    'this quarter', 'last year' or 'YTD'.
    """,
    parameters={"type": "object", "properties": {}},
)
def erp_company_context():
    today = frappe.utils.today()
    companies = frappe.get_list(
        "Company", fields=["name", "default_currency", "country"], limit_page_length=20
    )
    fy = frappe.get_all(
        "Fiscal Year",
        filters={"disabled": 0},
        fields=["name", "year_start_date", "year_end_date"],
        order_by="year_start_date desc",
        limit_page_length=3,
    )
    current = next(
        (f for f in fy if str(f.year_start_date) <= today <= str(f.year_end_date)), fy[0] if fy else None
    )
    return {
        "today": today,
        "companies": companies,
        "current_fiscal_year": current,
        "recent_fiscal_years": fy,
        "user": frappe.session.user,
        "roles": frappe.get_roles(),
    }
