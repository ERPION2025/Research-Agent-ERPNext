"""Web research tools backed by Tavily.

Search results are scored against a preferred-domain list on the way out, so
the source quality signal is available to the evaluator without a second
pass. This is the component-level eval from the course, moved inline.
"""

from __future__ import annotations

from urllib.parse import urlparse

import frappe
import requests
from frappe import _

from research_agent.agent.registry import tool

TAVILY_URL = "https://api.tavily.com"

DEFAULT_PREFERRED = {
    # Indian statutory and regulatory
    "rbi.org.in", "sebi.gov.in", "gst.gov.in", "incometax.gov.in", "mca.gov.in",
    "pib.gov.in", "niti.gov.in", "mospi.gov.in", "dgft.gov.in",
    # Global institutions
    "imf.org", "worldbank.org", "oecd.org", "wto.org", "un.org", "europa.eu",
    # Standards and research
    "arxiv.org", "nature.com", "science.org", "ieee.org", "acm.org",
    # Business press with a filing-grade record
    "reuters.com", "bloomberg.com", "ft.com", "economist.com",
    "livemint.com", "business-standard.com", "moneycontrol.com",
    # Vendor primary sources
    "frappe.io", "erpnext.com", "docs.erpnext.com",
}


def _key() -> str:
    s = frappe.get_cached_doc("Research Agent Settings")
    if not s.allow_web_search:
        frappe.throw(_("Web research is switched off in Research Agent Settings."), frappe.PermissionError)
    key = s.get_password("tavily_api_key", raise_exception=False)
    if not key:
        frappe.throw(_("No Tavily API key saved. Add it in Research Agent Settings."))
    return key


def preferred_domains() -> set[str]:
    s = frappe.get_cached_doc("Research Agent Settings")
    extra = {d.strip().lower() for d in (s.preferred_domains or "").split(",") if d.strip()}
    return DEFAULT_PREFERRED | extra


def score_sources(urls: list[str]) -> dict:
    """Ratio of results from preferred domains. Used by the evaluator."""
    prefs = preferred_domains()
    if not urls:
        return {"total": 0, "preferred": 0, "ratio": 0.0, "detail": []}
    detail, preferred = [], 0
    for u in urls:
        host = (urlparse(u).netloc or "").lower().removeprefix("www.")
        hit = any(host == p or host.endswith("." + p) for p in prefs)
        preferred += 1 if hit else 0
        detail.append({"url": u, "domain": host, "preferred": hit})
    return {
        "total": len(urls),
        "preferred": preferred,
        "ratio": round(preferred / len(urls), 3),
        "detail": detail,
    }


@tool(
    name="web_search",
    category="Web Research",
    description="""
    Search the live web for market, competitor, regulatory or benchmark information
    that is not in the ERP. Returns titles, URLs, published dates and content
    snippets, plus a source_quality block showing how many results came from
    preferred domains. Use specific queries with a year in them. Use this for
    outside-the-company facts only. Never use it for anything that should come from
    the ERP database.
    """,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query, 3 to 12 words, include the year for recency"},
            "max_results": {"type": "integer", "description": "1 to 10, default 5"},
            "depth": {"type": "string", "enum": ["basic", "advanced"], "description": "advanced costs more, use for hard questions"},
            "include_domains": {"type": "array", "items": {"type": "string"}},
            "days": {"type": "integer", "description": "Only results from the last N days"},
        },
        "required": ["query"],
    },
)
def web_search(
    query: str,
    max_results: int = 5,
    depth: str = "basic",
    include_domains: list[str] | None = None,
    days: int | None = None,
):
    s = frappe.get_cached_doc("Research Agent Settings")
    payload = {
        "api_key": _key(),
        "query": query,
        "search_depth": depth or s.tavily_search_depth or "basic",
        "max_results": min(int(max_results or 5), int(s.tavily_max_results or 10)),
        "include_answer": True,
        "include_raw_content": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if days:
        payload["days"] = int(days)
        payload["topic"] = "news"

    r = requests.post(f"{TAVILY_URL}/search", json=payload, timeout=45)
    r.raise_for_status()
    data = r.json()

    results = [
        {
            "title": x.get("title"),
            "url": x.get("url"),
            "published_date": x.get("published_date"),
            "score": x.get("score"),
            "content": (x.get("content") or "")[:1500],
        }
        for x in data.get("results", [])
    ]
    return {
        "query": query,
        "answer": data.get("answer"),
        "results": results,
        "source_quality": score_sources([x["url"] for x in results if x.get("url")]),
    }


@tool(
    name="web_fetch",
    category="Web Research",
    description="""
    Fetch and extract the readable text of a specific URL that web_search returned.
    Use this when a snippet looks decisive and you need the full number, table or
    quote behind it. One URL per call.
    """,
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
)
def web_fetch(url: str):
    r = requests.post(
        f"{TAVILY_URL}/extract",
        json={"api_key": _key(), "urls": [url]},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or []
    if not results:
        return {"url": url, "error": "Could not extract content", "failed": data.get("failed_results")}
    content = results[0].get("raw_content") or ""
    return {"url": url, "characters": len(content), "content": content[:20000]}
