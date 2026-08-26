"""Operational tools.

These exist because four questions get asked every single day and the
generic query tools answer them badly. A model left to build "what is my
collection today" out of erp_fetch_records will forget advances, miss journal
entries against the receivable account, and quietly include unreconciled POS
payments twice.

So each of these encodes the accounting once, correctly, and the model just
picks the right one:

  erp_sales_today       what did we sell
  erp_collections_today what did we actually get paid
  erp_production_today  what did we make
  erp_item_cost         what does a model cost us, three different ways
  erp_daily_pulse       all of the above in one call

Every one takes a date range, defaults to today, and states its own
assumptions in the payload so the agent can quote them in the answer.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, today

from research_agent.agent.registry import tool
from research_agent.compat import default_company, has_doctype, has_field, run_report

DATE_PROPS = {
    "from_date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
    "to_date": {"type": "string", "description": "YYYY-MM-DD. Defaults to from_date."},
    "company": {"type": "string", "description": "Defaults to the user's default company."},
}


def _range(from_date=None, to_date=None):
    f = getdate(from_date or today())
    t = getdate(to_date or f)
    if t < f:
        frappe.throw(_("to_date is before from_date."))
    return str(f), str(t)


def _company(company=None):
    c = company or default_company()
    if not c:
        frappe.throw(_("No company found. Pass one explicitly."))
    if not frappe.has_permission("Company", ptype="read"):
        frappe.throw(_("You cannot read Company records."), frappe.PermissionError)
    return c


def _perm(doctype: str) -> bool:
    """True when the doctype exists in this install and the user can read it."""
    return has_doctype(doctype) and frappe.has_permission(doctype, ptype="read")


PERMITTED_NAMES_CAP = 20000


def _permitted_names(doctype: str, filters: dict) -> list[str]:
    """Names of doctype rows this user may see, under the same rules
    frappe.get_list applies: role permissions, User Permissions (a Territory
    or Cost Center restriction, for example) and any permission_query_conditions
    hook another app on the site has registered.

    Several tools below drop to hand-written SQL for joins get_list cannot
    express (Sales Invoice Item, Sales Team, Journal Entry Account, Stock
    Entry Detail). frappe.has_permission(doctype) only proves the user may
    read *some* rows of that doctype, not these particular ones, so every
    query joining off one of these tables is scoped to this list rather than
    trusting the table name alone. That is what keeps a Territory-restricted
    user's breakdown restricted, the same as erp_fetch_records already is.
    """
    return frappe.get_list(
        doctype, filters=filters, pluck="name", limit_page_length=PERMITTED_NAMES_CAP, order_by=None
    )


# ---------------------------------------------------------------------------
# sales
# ---------------------------------------------------------------------------
@tool(
    name="erp_sales_today",
    category="ERPNext Operations",
    description="""
    What we sold in a period. Defaults to today.

    Returns invoiced sales from submitted Sales Invoices (net of returns and
    excluding tax), POS sales separately if POS Invoice is in use, order intake
    from Sales Orders for the same window, and a breakdown by item group,
    territory and salesperson.

    Use this for "what is my sales figure today", "how did we do this week",
    "sales this month so far". Do not rebuild this from erp_fetch_records.

    Invoiced sales and order intake are different numbers and both are returned.
    Say which one you are quoting.
    """,
    parameters={"type": "object", "properties": {**DATE_PROPS,
                "breakdown_by": {"type": "string",
                                 "enum": ["item_group", "territory", "sales_person", "customer", "brand", "none"],
                                 "description": "Default item_group"}}},
)
def erp_sales_today(from_date=None, to_date=None, company=None, breakdown_by="item_group"):
    f, t = _range(from_date, to_date)
    co = _company(company)
    out = {
        "period": {"from": f, "to": t},
        "company": co,
        "basis": "Submitted Sales Invoices only. is_return invoices are included as negatives, "
                 "so the total is net of credit notes. Amounts are net_total (excluding tax) "
                 "and grand_total (including tax); both are given.",
    }

    if not _perm("Sales Invoice"):
        frappe.throw(_("You cannot read Sales Invoice."), frappe.PermissionError)

    base_filters = {"docstatus": 1, "company": co, "posting_date": ["between", [f, t]]}

    summary = frappe.get_list(
        "Sales Invoice",
        filters=base_filters,
        fields=[
            "count(name) as invoices",
            "sum(base_net_total) as net_total",
            "sum(base_grand_total) as grand_total",
            "sum(base_total_taxes_and_charges) as taxes",
        ],
    )
    row = summary[0] if summary else {}
    out["invoiced"] = {
        "invoices": int(row.get("invoices") or 0),
        "net_of_tax": flt(row.get("net_total")),
        "including_tax": flt(row.get("grand_total")),
        "taxes": flt(row.get("taxes")),
    }

    returns = frappe.get_list(
        "Sales Invoice",
        filters={**base_filters, "is_return": 1},
        fields=["count(name) as cnt", "sum(base_grand_total) as amt"],
    )
    r = returns[0] if returns else {}
    out["returns"] = {"count": int(r.get("cnt") or 0), "amount": abs(flt(r.get("amt")))}

    # POS runs through a separate doctype until consolidated
    if _perm("POS Invoice"):
        pos = frappe.get_list(
            "POS Invoice",
            filters=base_filters,
            fields=["count(name) as cnt", "sum(base_grand_total) as amt"],
        )
        p = pos[0] if pos else {}
        if p.get("cnt"):
            out["pos_unconsolidated"] = {
                "invoices": int(p.get("cnt") or 0),
                "including_tax": flt(p.get("amt")),
                "note": "POS Invoices not yet consolidated into Sales Invoices. "
                        "Do not add to invoiced without saying so.",
            }

    if _perm("Sales Order"):
        so = frappe.get_list(
            "Sales Order",
            filters={"docstatus": 1, "company": co, "transaction_date": ["between", [f, t]]},
            fields=["count(name) as cnt", "sum(base_grand_total) as amt"],
        )
        s = so[0] if so else {}
        out["order_intake"] = {
            "orders": int(s.get("cnt") or 0),
            "value_including_tax": flt(s.get("amt")),
            "note": "Order intake, not revenue. Different number from invoiced.",
        }

    if breakdown_by and breakdown_by != "none":
        out["breakdown"] = _sales_breakdown(breakdown_by, co, f, t)

    return out


def _sales_breakdown(dimension: str, company: str, f: str, t: str):
    if dimension in ("territory", "customer"):
        field = dimension
        rows = frappe.get_list(
            "Sales Invoice",
            filters={"docstatus": 1, "company": company, "posting_date": ["between", [f, t]]},
            fields=[f"`{field}` as label", "sum(base_net_total) as value", "count(name) as invoices"],
            group_by=f"`{field}`",
            order_by="value desc",
            limit_page_length=25,
        )
        return {"dimension": dimension, "rows": rows}

    base_filters = {"docstatus": 1, "company": company, "posting_date": ["between", [f, t]]}
    permitted = _permitted_names("Sales Invoice", base_filters)
    if not permitted:
        return {"dimension": dimension, "rows": []}

    if dimension == "sales_person":
        if not _perm("Sales Team"):
            return {"dimension": dimension, "rows": [], "note": "Sales Team not readable."}
        rows = frappe.db.sql(
            """
            select st.sales_person as label,
                   sum(st.allocated_amount) as value
            from `tabSales Team` st
            join `tabSales Invoice` si on si.name = st.parent
            where si.name in %(names)s
            group by st.sales_person order by value desc limit 25
            """,
            {"names": permitted},
            as_dict=True,
        )
        return {"dimension": dimension, "rows": rows,
                "note": "Allocated amounts from the Sales Team table, so this sums to invoice value "
                        "only when allocation is 100 percent."}

    # item_group / brand come off the item rows
    field = "ii.item_group" if dimension == "item_group" else "ii.brand"
    rows = frappe.db.sql(
        f"""
        select {field} as label,
               sum(ii.base_net_amount) as value,
               sum(ii.stock_qty) as qty
        from `tabSales Invoice Item` ii
        join `tabSales Invoice` si on si.name = ii.parent
        where si.name in %(names)s
        group by {field} order by value desc limit 25
        """,
        {"names": permitted},
        as_dict=True,
    )
    return {"dimension": dimension, "rows": rows}


# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------
@tool(
    name="erp_collections_today",
    category="ERPNext Operations",
    description="""
    What money actually came in during a period. Defaults to today.

    Collections are not sales. This counts submitted Payment Entries of type
    Receive from customers, plus Journal Entries that credit a receivable
    account, plus cash and card paid directly against invoices at the counter.
    Advances are included and flagged separately, because an advance is cash in
    but not revenue.

    Returns a mode-of-payment split so you can answer "how much of today's
    collection was UPI".

    Use this for "what is my collection today", "how much did we collect this
    week", "cash position today".
    """,
    parameters={"type": "object", "properties": {**DATE_PROPS,
                "include_supplier_payments": {"type": "boolean",
                                              "description": "Also return money paid out. Default false."}}},
)
def erp_collections_today(from_date=None, to_date=None, company=None, include_supplier_payments=False):
    f, t = _range(from_date, to_date)
    co = _company(company)
    out = {
        "period": {"from": f, "to": t},
        "company": co,
        "basis": "Submitted Payment Entries (Receive, party type Customer), plus Journal Entries "
                 "crediting a Receivable account, plus amounts paid directly on invoices via the "
                 "Payments table. Advances are included and shown separately.",
    }

    if not _perm("Payment Entry"):
        frappe.throw(_("You cannot read Payment Entry."), frappe.PermissionError)

    pe = frappe.get_list(
        "Payment Entry",
        filters={
            "docstatus": 1,
            "company": co,
            "payment_type": "Receive",
            "party_type": "Customer",
            "posting_date": ["between", [f, t]],
        },
        fields=[
            "count(name) as cnt",
            "sum(base_received_amount) as amount",
            "sum(unallocated_amount) as unallocated",
        ],
    )
    p = pe[0] if pe else {}
    out["payment_entries"] = {
        "count": int(p.get("cnt") or 0),
        "amount": flt(p.get("amount")),
        "unallocated_advance": flt(p.get("unallocated")),
    }

    out["by_mode_of_payment"] = frappe.get_list(
        "Payment Entry",
        filters={
            "docstatus": 1,
            "company": co,
            "payment_type": "Receive",
            "party_type": "Customer",
            "posting_date": ["between", [f, t]],
        },
        fields=["mode_of_payment as label", "sum(base_received_amount) as value", "count(name) as count"],
        group_by="mode_of_payment",
        order_by="value desc",
    )

    # cash and card taken straight on the invoice
    if _perm("Sales Invoice Payment"):
        invoice_names = _permitted_names(
            "Sales Invoice", {"docstatus": 1, "company": co, "posting_date": ["between", [f, t]]}
        )
        if invoice_names:
            counter = frappe.db.sql(
                """
                select sip.mode_of_payment as label, sum(sip.base_amount) as value
                from `tabSales Invoice Payment` sip
                join `tabSales Invoice` si on si.name = sip.parent
                where si.name in %(names)s
                group by sip.mode_of_payment
                """,
                {"names": invoice_names},
                as_dict=True,
            )
            if counter:
                out["paid_on_invoice"] = {
                    "rows": counter,
                    "total": sum(flt(r.value) for r in counter),
                    "note": "Collected at the counter against the invoice itself, not via a Payment Entry.",
                }

    # journal entries hitting receivable
    if _perm("Journal Entry"):
        je_names = _permitted_names(
            "Journal Entry", {"docstatus": 1, "company": co, "posting_date": ["between", [f, t]]}
        )
        if je_names:
            je = frappe.db.sql(
                """
                select sum(jea.credit_in_account_currency) as amount, count(distinct je.name) as cnt
                from `tabJournal Entry Account` jea
                join `tabJournal Entry` je on je.name = jea.parent
                join `tabAccount` a on a.name = jea.account
                where je.name in %(names)s
                  and a.account_type = 'Receivable'
                  and jea.credit_in_account_currency > 0
                """,
                {"names": je_names},
                as_dict=True,
            )
            j = je[0] if je else {}
            if flt(j.get("amount")):
                out["journal_entries_against_receivable"] = {
                    "count": int(j.get("cnt") or 0),
                    "amount": flt(j.get("amount")),
                    "note": "Includes write-offs and adjustments, not only cash. Check before adding to collections.",
                }

    out["headline_collection"] = flt(out["payment_entries"]["amount"]) + flt(
        (out.get("paid_on_invoice") or {}).get("total")
    )

    if include_supplier_payments and _perm("Payment Entry"):
        pay = frappe.get_list(
            "Payment Entry",
            filters={
                "docstatus": 1,
                "company": co,
                "payment_type": "Pay",
                "posting_date": ["between", [f, t]],
            },
            fields=["count(name) as cnt", "sum(base_paid_amount) as amount"],
        )
        q = pay[0] if pay else {}
        out["paid_out"] = {"count": int(q.get("cnt") or 0), "amount": flt(q.get("amount"))}
        out["net_cash_movement"] = out["headline_collection"] - flt(q.get("amount"))

    return out


# ---------------------------------------------------------------------------
# production
# ---------------------------------------------------------------------------
@tool(
    name="erp_production_today",
    category="ERPNext Operations",
    description="""
    What we manufactured in a period. Defaults to today.

    Counts finished goods actually produced, taken from submitted Stock Entries
    of purpose Manufacture, which is the only place production is real. Also
    returns Work Order progress (started, completed, pending), a per-item and
    per-model breakdown, scrap and rejection quantities, and Job Card completion
    if the routing is in use.

    Use this for "how many bikes did we manufacture today", "production this
    week", "which models are behind schedule".

    Work Order completed_qty and Stock Entry manufactured quantity can differ
    when a work order spans days. Both are returned. Say which one you quote.
    """,
    parameters={"type": "object", "properties": {**DATE_PROPS,
                "item_group": {"type": "string", "description": "Restrict to one item group, e.g. 'Bikes'"}}},
)
def erp_production_today(from_date=None, to_date=None, company=None, item_group=None):
    f, t = _range(from_date, to_date)
    co = _company(company)

    if not has_doctype("Work Order"):
        return {"error": "The Manufacturing module is not installed on this site."}

    out = {
        "period": {"from": f, "to": t},
        "company": co,
        "basis": "Finished quantity from submitted Stock Entries with purpose 'Manufacture', "
                 "counting only rows flagged is_finished_item. Raw material consumption and "
                 "scrap rows are excluded. Material Transfer for Manufacture is excluded, since "
                 "moving components to the shop floor is not production. Work Order figures are "
                 "cumulative on the order, not per day.",
    }

    if not _perm("Stock Entry"):
        frappe.throw(_("You cannot read Stock Entry."), frappe.PermissionError)

    # Filter on `purpose`, not `stock_entry_type`. stock_entry_type is a Link to a
    # user-editable master, so a site can rename or add types. purpose is the
    # select field ERPNext itself branches on when posting the ledger, which makes
    # it the thing that is actually true.

    se_names = _permitted_names(
        "Stock Entry",
        {"docstatus": 1, "company": co, "purpose": "Manufacture", "posting_date": ["between", [f, t]]},
    )
    out["produced"] = {"total_qty": 0, "distinct_items": 0, "total_valuation": 0, "rows": []}

    if se_names:
        ig_join = "join `tabItem` it on it.name = sed.item_code" if item_group else ""
        ig_where = "and it.item_group = %(ig)s" if item_group else ""
        params = {"names": se_names, "ig": item_group}

        produced = frappe.db.sql(
            f"""
            select sed.item_code as item_code, sed.item_name as item_name,
                   sum(sed.transfer_qty) as qty, sed.stock_uom as uom,
                   sum(sed.amount) as valuation,
                   count(distinct se.name) as entries
            from `tabStock Entry Detail` sed
            join `tabStock Entry` se on se.name = sed.parent
            {ig_join}
            where se.name in %(names)s
              and sed.is_finished_item = 1
              {ig_where}
            group by sed.item_code, sed.item_name, sed.stock_uom
            order by qty desc
            """,
            params,
            as_dict=True,
        )
        out["produced"] = {
            "total_qty": sum(flt(r.qty) for r in produced),
            "distinct_items": len(produced),
            "total_valuation": sum(flt(r.valuation) for r in produced),
            "rows": produced,
        }

        # Cross-check. ERPNext validates that a Manufacture entry has at least
        # one is_finished_item row, so the two counts should always agree. If
        # they do not, something upstream is off and a silently wrong
        # production number is worse than a loud one, so surface it rather
        # than absorbing it.
        inbound_only = frappe.db.sql(
            """
            select sum(sed.transfer_qty) as qty
            from `tabStock Entry Detail` sed
            join `tabStock Entry` se on se.name = sed.parent
            where se.name in %(names)s
              and ifnull(sed.s_warehouse, '') = '' and ifnull(sed.t_warehouse, '') != ''
              and ifnull(sed.is_scrap_item, 0) = 0
            """,
            {"names": se_names},
            as_dict=True,
        )
        cross = flt((inbound_only[0] if inbound_only else {}).get("qty"))
        flagged = flt(out["produced"]["total_qty"])
        if abs(cross - flagged) > 0.001:
            out["produced"]["discrepancy"] = {
                "flagged_as_finished_item": flagged,
                "inbound_non_scrap_rows": cross,
                "note": "These should match. Report the flagged figure as production and mention "
                        "the gap; some Manufacture entries may have finished goods not marked "
                        "is_finished_item.",
            }

        scrap = frappe.db.sql(
            """
            select sed.item_code, sum(sed.transfer_qty) as qty
            from `tabStock Entry Detail` sed
            join `tabStock Entry` se on se.name = sed.parent
            where se.name in %(names)s
              and sed.is_scrap_item = 1
            group by sed.item_code order by qty desc limit 20
            """,
            {"names": se_names},
            as_dict=True,
        )
        if scrap:
            out["scrap"] = {"rows": scrap, "total_qty": sum(flt(r.qty) for r in scrap)}

    if _perm("Work Order"):
        wo_filters = {"company": co, "docstatus": 1}
        statuses = frappe.get_list(
            "Work Order",
            filters=wo_filters,
            fields=["status", "count(name) as count", "sum(qty) as planned_qty",
                    "sum(produced_qty) as produced_qty"],
            group_by="status",
        )
        out["work_orders_open"] = {"by_status": statuses}

        completed = frappe.get_list(
            "Work Order",
            filters={**wo_filters, "status": "Completed",
                     "actual_end_date": ["between", [f, t]]},
            fields=["name", "production_item", "item_name", "qty", "produced_qty", "actual_end_date"],
            order_by="actual_end_date desc",
            limit_page_length=50,
        )
        out["work_orders_completed_in_period"] = {"count": len(completed), "rows": completed}

        started = frappe.db.count(
            "Work Order",
            {"company": co, "docstatus": 1, "actual_start_date": ["between", [f, t]]},
        )
        out["work_orders_started_in_period"] = started

        overdue = frappe.get_list(
            "Work Order",
            filters={"company": co, "docstatus": 1, "status": ["in", ["In Process", "Not Started"]],
                     "planned_end_date": ["<", today()]},
            fields=["name", "production_item", "qty", "produced_qty", "planned_end_date", "status"],
            order_by="planned_end_date asc",
            limit_page_length=25,
        )
        if overdue:
            out["overdue_work_orders"] = {"count": len(overdue), "rows": overdue}

    if _perm("Job Card") and has_field("Job Card", "total_completed_qty"):
        jc = frappe.get_list(
            "Job Card",
            filters={"company": co, "docstatus": 1, "modified": ["between", [f, add_days(t, 1)]]},
            fields=["workstation as label", "sum(total_completed_qty) as value", "count(name) as cards"],
            group_by="workstation",
            order_by="value desc",
            limit_page_length=20,
        )
        if jc:
            out["job_cards_by_workstation"] = jc

    return out


# ---------------------------------------------------------------------------
# item cost / COGS
# ---------------------------------------------------------------------------
@tool(
    name="erp_item_cost",
    category="ERPNext Operations",
    description="""
    What a product actually costs us. Three different numbers, because "COGS"
    means three different things depending on who is asking:

    1. valuation_rate  - current moving-average or FIFO cost sitting in stock.
       This is the balance sheet number.
    2. bom_cost        - what the active BOM says it should cost, split into raw
       material, operating and scrap credit. This is the engineering number.
    3. realised_cogs   - what it actually cost on the units we sold in the
       period, from the Gross Profit report. This is the P&L number, and it is
       the one that moves margin.

    Use this for "what is the current COGS on the bike models", "why is the
    Model X margin down", "BOM cost vs actual".

    Pass item_code for one model, or item_group / brand for a family. If the
    three numbers disagree by more than a few percent, that gap is the answer
    and you should say so.
    """,
    parameters={
        "type": "object",
        "properties": {
            "item_code": {"type": "string"},
            "item_group": {"type": "string", "description": "e.g. 'Bikes'. Use when no single item is named."},
            "brand": {"type": "string"},
            "from_date": {"type": "string", "description": "For realised COGS. Defaults to start of current month."},
            "to_date": {"type": "string"},
            "company": {"type": "string"},
            "include_bom_breakup": {"type": "boolean", "description": "Return the BOM line items. Default true."},
        },
    },
)
def erp_item_cost(
    item_code=None,
    item_group=None,
    brand=None,
    from_date=None,
    to_date=None,
    company=None,
    include_bom_breakup=True,
):
    if not (item_code or item_group or brand):
        frappe.throw(_("Pass item_code, item_group or brand."))
    co = _company(company)
    f = str(getdate(from_date)) if from_date else str(getdate(today()).replace(day=1))
    t = str(getdate(to_date or today()))

    if not _perm("Item"):
        frappe.throw(_("You cannot read Item."), frappe.PermissionError)

    item_filters = {"disabled": 0, "is_stock_item": 1}
    if item_code:
        item_filters["name"] = item_code
    if item_group:
        item_filters["item_group"] = item_group
    if brand:
        item_filters["brand"] = brand

    items = frappe.get_list(
        "Item",
        filters=item_filters,
        fields=["name", "item_name", "item_group", "brand", "stock_uom", "last_purchase_rate"],
        limit_page_length=40,
        order_by="name asc",
    )
    if not items:
        return {"items": [], "note": "No matching items. Check the item group or brand spelling."}

    codes = [i.name for i in items]
    out = {
        "period_for_realised_cogs": {"from": f, "to": t},
        "company": co,
        "basis": "valuation_rate is the current Bin value. bom_cost is the active default BOM. "
                 "realised_cogs comes from the Gross Profit report for the period.",
        "items": [],
    }

    # 1. current valuation from Bin
    valuations = {}
    if _perm("Bin"):
        for r in frappe.get_list(
            "Bin",
            filters={"item_code": ["in", codes]},
            fields=["item_code", "sum(actual_qty) as qty", "sum(stock_value) as value"],
            group_by="item_code",
        ):
            qty = flt(r.get("qty"))
            valuations[r.item_code] = {
                "qty_on_hand": qty,
                "stock_value": flt(r.get("value")),
                "valuation_rate": flt(r.get("value")) / qty if qty else 0.0,
            }

    # 2. active BOM cost
    boms = {}
    if has_doctype("BOM") and _perm("BOM"):
        for b in frappe.get_list(
            "BOM",
            filters={"item": ["in", codes], "is_active": 1, "is_default": 1, "docstatus": 1},
            fields=["name", "item", "quantity", "raw_material_cost", "operating_cost",
                    "scrap_material_cost", "total_cost", "currency", "modified"],
        ):
            per_unit = flt(b.quantity) or 1
            boms[b.item] = {
                "bom": b.name,
                "for_quantity": flt(b.quantity),
                "raw_material_cost_per_unit": flt(b.raw_material_cost) / per_unit,
                "operating_cost_per_unit": flt(b.operating_cost) / per_unit,
                "scrap_credit_per_unit": flt(b.scrap_material_cost) / per_unit,
                "total_cost_per_unit": flt(b.total_cost) / per_unit,
                "currency": b.currency,
                "last_updated": str(b.modified),
            }

    # 3. realised COGS from the Gross Profit report
    realised = _realised_cogs(co, f, t, codes)

    for i in items:
        entry = {
            "item_code": i.name,
            "item_name": i.item_name,
            "item_group": i.item_group,
            "brand": i.brand,
            "uom": i.stock_uom,
            "last_purchase_rate": flt(i.last_purchase_rate),
            "current_valuation": valuations.get(i.name),
            "bom_cost": boms.get(i.name),
            "realised_cogs": realised.get(i.name),
        }
        v = (entry["current_valuation"] or {}).get("valuation_rate")
        b = (entry["bom_cost"] or {}).get("total_cost_per_unit")
        if v and b:
            entry["valuation_vs_bom_variance_pct"] = round((v - b) / b * 100, 2)
        out["items"].append(entry)

    if include_bom_breakup and item_code and boms.get(item_code):
        out["bom_lines"] = frappe.get_list(
            "BOM Item",
            filters={"parent": boms[item_code]["bom"]},
            fields=["item_code", "item_name", "qty", "uom", "rate", "amount"],
            order_by="amount desc",
            limit_page_length=100,
        )

    return out


def _realised_cogs(company: str, f: str, t: str, codes: list[str]) -> dict:
    """Item-wise COGS on units actually sold, via the Gross Profit report."""
    if not frappe.db.exists("Report", "Gross Profit"):
        return {}
    try:
        report = frappe.get_doc("Report", "Gross Profit")
        if not frappe.has_permission("Report", ptype="read", doc=report):
            return {}
        _columns, rows = run_report(
            report,
            {"company": company, "from_date": f, "to_date": t, "group_by": "Item Code"},
        )
    except Exception:
        frappe.log_error(title="Research Agent: Gross Profit report failed",
                         message=frappe.get_traceback())
        return {}

    wanted = set(codes)
    out = {}
    for r in rows or []:
        code = r.get("item_code") if isinstance(r, dict) else None
        if not code or code not in wanted:
            continue
        qty = flt(r.get("qty"))
        cogs = flt(r.get("buying_amount"))
        out[code] = {
            "qty_sold": qty,
            "revenue": flt(r.get("base_amount") or r.get("selling_amount")),
            "cogs_total": cogs,
            "cogs_per_unit": cogs / qty if qty else 0.0,
            "gross_profit": flt(r.get("gross_profit")),
            "gross_margin_pct": flt(r.get("gross_profit_percent")),
        }
    return out


# ---------------------------------------------------------------------------
# the morning call
# ---------------------------------------------------------------------------
@tool(
    name="erp_daily_pulse",
    category="ERPNext Operations",
    description="""
    One call for the whole morning snapshot: sales, collections, production and
    receivables for a day, with the same day last week and the month to date for
    context. Use this when someone asks "how are we doing today", "give me the
    daily numbers" or asks for a daily dashboard, instead of making four
    separate calls.

    Returns comparison figures so you can state a change, not just a level.
    """,
    parameters={"type": "object", "properties": {
        "date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
        "company": {"type": "string"},
    }},
)
def erp_daily_pulse(date=None, company=None):
    d = str(getdate(date or today()))
    co = _company(company)
    last_week = str(add_days(getdate(d), -7))
    month_start = str(getdate(d).replace(day=1))

    def safe(fn, **kw):
        try:
            return fn(**kw)
        except frappe.PermissionError as e:
            return {"restricted": str(e)}

    pulse = {
        "date": d,
        "company": co,
        "today": {
            "sales": safe(erp_sales_today, from_date=d, company=co, breakdown_by="item_group"),
            "collections": safe(erp_collections_today, from_date=d, company=co),
            "production": safe(erp_production_today, from_date=d, company=co),
        },
        "same_day_last_week": {
            "sales": safe(erp_sales_today, from_date=last_week, company=co, breakdown_by="none"),
            "collections": safe(erp_collections_today, from_date=last_week, company=co),
            "production": safe(erp_production_today, from_date=last_week, company=co),
        },
        "month_to_date": {
            "sales": safe(erp_sales_today, from_date=month_start, to_date=d, company=co, breakdown_by="none"),
            "collections": safe(erp_collections_today, from_date=month_start, to_date=d, company=co),
        },
    }

    if _perm("Sales Invoice"):
        overdue = frappe.get_list(
            "Sales Invoice",
            filters={"docstatus": 1, "company": co, "outstanding_amount": [">", 0],
                     "due_date": ["<", d]},
            fields=["count(name) as cnt", "sum(outstanding_amount) as amt"],
        )
        o = overdue[0] if overdue else {}
        pulse["overdue_receivables"] = {
            "invoices": int(o.get("cnt") or 0),
            "amount": flt(o.get("amt")),
        }

    return pulse
