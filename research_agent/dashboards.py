"""Dashboard connections.

Research that lives only inside the agent is research nobody reads twice.
These hooks put Market Research Report on the connections tab of Item, Brand
and Item Group, so a product manager opening a bike model sees the pricing
research next to the BOM and the price list.

Each function extends the existing ERPNext dashboard rather than replacing
it, because replacing it would quietly delete the standard connections.
"""

from __future__ import annotations

import frappe
from frappe import _


def _extend(base_getter, fieldname: str, data=None) -> dict:
    try:
        base = base_getter(data) if data is not None else base_getter()
    except TypeError:
        base = base_getter()
    base = base or {}

    base.setdefault("transactions", [])
    base["transactions"].append(
        {"label": _("Research"), "items": ["Market Research Report"]}
    )

    # ERPNext dashboards key every connection off one fieldname. When it does
    # not match ours, declare the difference explicitly instead of silently
    # showing the wrong records.
    if base.get("fieldname") and base["fieldname"] != fieldname:
        base.setdefault("non_standard_fieldnames", {})
        base["non_standard_fieldnames"]["Market Research Report"] = fieldname
    else:
        base["fieldname"] = fieldname

    return base


def item_dashboard(data=None):
    from erpnext.stock.doctype.item.item_dashboard import get_data

    return _extend(get_data, "item_code", data)


def brand_dashboard(data=None):
    def empty(_d=None):
        return {"fieldname": "brand", "transactions": []}

    try:
        from erpnext.setup.doctype.brand.brand_dashboard import get_data
    except ImportError:
        get_data = empty
    return _extend(get_data, "brand", data)


def item_group_dashboard(data=None):
    def empty(_d=None):
        return {"fieldname": "item_group", "transactions": []}

    try:
        from erpnext.setup.doctype.item_group.item_group_dashboard import get_data
    except ImportError:
        get_data = empty
    return _extend(get_data, "item_group", data)


def clear_research_cache(doc, method=None):
    """Bust the item cache when research is submitted so the dashboard count
    is right on the next load."""
    for field in ("item_code", "brand", "item_group"):
        value = doc.get(field)
        if value:
            frappe.cache().hdel("dashboard_info", value)
