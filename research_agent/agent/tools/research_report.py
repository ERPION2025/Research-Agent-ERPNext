"""Market Research Report.

A Research Session is a conversation. It expires, it holds tool logs, it gets
purged after ninety days. That is the wrong container for a piece of research
that a pricing decision will be justified by six months later.

So findings get published into Market Research Report: a proper submittable
ERPNext document with a naming series, an approval trail, and links to the
masters the research is actually about. Item, Item Group, Brand, Territory,
Customer Group, Supplier Group. That means the research shows up on the Item
dashboard next to the BOM and the price list, which is where someone looking
at a model will actually find it.

Sessions are working memory. Reports are the record.
"""

from __future__ import annotations

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import today

from research_agent.agent.registry import tool
from research_agent.agent.tools.tavily_search import preferred_domains
from research_agent.compat import default_company


@tool(
    name="publish_research_report",
    category="Research",
    description="""
    Save the research you just did as a Market Research Report, a permanent
    ERPNext document linked to the items, brands or territories it is about.

    Call this at the end of any question that involved real external research:
    competitor pricing, market sizing, regulatory change, supplier landscape,
    demand signals. Do not call it for routine internal reporting like today's
    sales figure.

    Link it properly. If the research is about a specific bike model, set
    item_code. If it is about a family, set item_group or brand. A report with
    no links is a report nobody will find again.

    Findings must each carry the source URL they came from. A finding without a
    source will be rejected.

    The report is created as a draft. A human submits it. Tell the user the
    report name so they can open it.
    """,
    parameters={
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "One line, specific. 'Competitor pricing for 125cc commuter models, Pune, Aug 2026'"},
            "research_type": {
                "type": "string",
                "enum": ["Market Sizing", "Competitor", "Pricing", "Regulatory",
                         "Customer", "Supplier", "Technology", "Demand", "Other"],
            },
            "executive_summary": {"type": "string", "description": "Three to six lines a CXO can read alone. Markdown."},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding": {"type": "string"},
                        "category": {"type": "string", "enum": ["Market", "Pricing", "Competitor",
                                                                "Regulatory", "Demand", "Supply", "Risk", "Other"]},
                        "impact": {"type": "string", "enum": ["High", "Medium", "Low"]},
                        "confidence": {"type": "number", "description": "0 to 1"},
                        "evidence": {"type": "string", "description": "The number or quote it rests on"},
                        "source_url": {"type": "string"},
                    },
                    "required": ["finding", "source_url"],
                },
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "published_date": {"type": "string"},
                        "snippet": {"type": "string"},
                    },
                    "required": ["url"],
                },
            },
            "competitors": {
                "type": "array",
                "description": "Only for competitor or pricing research",
                "items": {
                    "type": "object",
                    "properties": {
                        "competitor_name": {"type": "string"},
                        "model_or_product": {"type": "string"},
                        "observed_price": {"type": "number"},
                        "currency": {"type": "string"},
                        "positioning": {"type": "string"},
                        "source_url": {"type": "string"},
                    },
                    "required": ["competitor_name"],
                },
            },
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "recommendation": {"type": "string"},
                        "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                        "expected_impact": {"type": "string"},
                        "owner_role": {"type": "string", "description": "Which function should act, e.g. 'Pricing', 'Sales'"},
                    },
                    "required": ["recommendation"],
                },
            },
            "item_code": {"type": "string", "description": "Link to a specific Item when the research is model-specific"},
            "item_group": {"type": "string"},
            "brand": {"type": "string"},
            "territory": {"type": "string"},
            "customer_group": {"type": "string"},
            "supplier_group": {"type": "string"},
            "period_from": {"type": "string", "description": "YYYY-MM-DD, the window the research covers"},
            "period_to": {"type": "string"},
        },
        "required": ["subject", "research_type", "executive_summary", "findings"],
    },
)
def publish_research_report(
    subject: str,
    research_type: str,
    executive_summary: str,
    findings: list,
    sources: list | None = None,
    competitors: list | None = None,
    recommendations: list | None = None,
    item_code: str | None = None,
    item_group: str | None = None,
    brand: str | None = None,
    territory: str | None = None,
    customer_group: str | None = None,
    supplier_group: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    context: dict | None = None,
):
    if not frappe.has_permission("Market Research Report", ptype="create"):
        frappe.throw(_("You cannot create Market Research Reports."), frappe.PermissionError)

    findings = findings or []
    unsourced = [f for f in findings if not (f.get("source_url") or "").strip()]
    if unsourced:
        frappe.throw(
            _("{0} findings have no source_url. Every finding needs the URL it came from.").format(len(unsourced))
        )

    _validate_link("Item", item_code)
    _validate_link("Item Group", item_group)
    _validate_link("Brand", brand)
    _validate_link("Territory", territory)
    _validate_link("Customer Group", customer_group)
    _validate_link("Supplier Group", supplier_group)

    session = (context or {}).get("session")
    prefs = preferred_domains()

    # dedupe sources, and fold in any URL that only appears on a finding
    seen, source_rows = set(), []
    for s in list(sources or []) + [{"url": f["source_url"]} for f in findings]:
        url = (s.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        host = (urlparse(url).netloc or "").lower().removeprefix("www.")
        source_rows.append(
            {
                "url": url[:500],
                "title": (s.get("title") or "")[:250],
                "domain": host[:140],
                "published_date": s.get("published_date"),
                "is_preferred": 1 if any(host == p or host.endswith("." + p) for p in prefs) else 0,
                "snippet": (s.get("snippet") or "")[:1000],
            }
        )

    preferred_ratio = (
        sum(r["is_preferred"] for r in source_rows) / len(source_rows) if source_rows else 0.0
    )
    confidences = [float(f.get("confidence") or 0.6) for f in findings]

    doc = frappe.get_doc(
        {
            "doctype": "Market Research Report",
            "subject": subject[:250],
            "research_type": research_type,
            "company": default_company(),
            "research_date": today(),
            "period_from": period_from,
            "period_to": period_to,
            "executive_summary": executive_summary,
            "item_code": item_code,
            "item_group": item_group,
            "brand": brand,
            "territory": territory,
            "customer_group": customer_group,
            "supplier_group": supplier_group,
            "research_session": session.name if session else None,
            "originating_prompt": (session.prompt if session else "")[:1000],
            "confidence_score": round(sum(confidences) / len(confidences), 3) if confidences else 0,
            "source_quality_score": round(preferred_ratio, 3),
            "key_findings": [
                {
                    "finding": (f.get("finding") or "")[:1000],
                    "category": f.get("category") or "Other",
                    "impact": f.get("impact") or "Medium",
                    "confidence": float(f.get("confidence") or 0.6),
                    "evidence": (f.get("evidence") or "")[:1000],
                    "source_url": (f.get("source_url") or "")[:500],
                }
                for f in findings
            ],
            "sources": source_rows,
            "competitor_observations": [
                {
                    "competitor_name": (c.get("competitor_name") or "")[:140],
                    "model_or_product": (c.get("model_or_product") or "")[:250],
                    "observed_price": c.get("observed_price"),
                    "currency": c.get("currency"),
                    "positioning": (c.get("positioning") or "")[:500],
                    "source_url": (c.get("source_url") or "")[:500],
                    "observed_on": today(),
                }
                for c in (competitors or [])
            ],
            "recommendations": [
                {
                    "recommendation": (r.get("recommendation") or "")[:1000],
                    "priority": r.get("priority") or "Medium",
                    "expected_impact": (r.get("expected_impact") or "")[:500],
                    "owner_role": (r.get("owner_role") or "")[:140],
                }
                for r in (recommendations or [])
            ],
        }
    ).insert()
    frappe.db.commit()

    return {
        "report": doc.name,
        "status": "Draft",
        "url": f"/app/market-research-report/{doc.name}",
        "findings": len(findings),
        "sources": len(source_rows),
        "source_quality_score": round(preferred_ratio, 3),
        "note": "Saved as a draft. A human needs to submit it. Give the user the report name.",
    }


def _validate_link(doctype: str, value: str | None):
    if value and not frappe.db.exists(doctype, value):
        frappe.throw(_("{0} '{1}' does not exist. Check the spelling or leave it blank.").format(doctype, value))


@tool(
    name="find_research_reports",
    category="Research",
    description="""
    Search past Market Research Reports before doing new research. If someone
    already looked into competitor pricing on this model last month, read that
    first and build on it rather than starting from nothing.

    Returns subject, date, type, links and the executive summary.
    """,
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string"},
            "research_type": {"type": "string"},
            "item_code": {"type": "string"},
            "item_group": {"type": "string"},
            "brand": {"type": "string"},
            "since": {"type": "string", "description": "YYYY-MM-DD"},
        },
    },
)
def find_research_reports(keyword=None, research_type=None, item_code=None,
                          item_group=None, brand=None, since=None):
    filters = {"docstatus": ["<", 2]}
    for field, value in (("research_type", research_type), ("item_code", item_code),
                         ("item_group", item_group), ("brand", brand)):
        if value:
            filters[field] = value
    if since:
        filters["research_date"] = [">=", since]

    or_filters = None
    if keyword:
        or_filters = {
            "subject": ["like", f"%{keyword}%"],
            "executive_summary": ["like", f"%{keyword}%"],
        }

    rows = frappe.get_list(
        "Market Research Report",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "subject", "research_type", "research_date", "item_code",
                "item_group", "brand", "confidence_score", "source_quality_score", "docstatus"],
        order_by="research_date desc",
        limit_page_length=15,
    )
    for r in rows:
        r["executive_summary"] = frappe.db.get_value("Market Research Report", r.name, "executive_summary")
        r["status"] = {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(r.pop("docstatus"), "Draft")
    return {"reports": rows, "count": len(rows)}
