# Changelog

## 0.1.0 - unreleased

First cut.

**Core**
- Reflexion loop: plan, act, compose, evaluate, reflect, retry. Best-scoring draft wins.
- Five-check evaluator: grounding, ERP grounding, artifacts, source quality, LLM judge.
- Provider router for OpenAI and Anthropic with three model slots.
- Tested against Frappe and ERPNext v15 and v16, with differences isolated in compat.py.

**ERPNext data**
- Permission-aware generic reads: fetch, describe, run report, validated read-only SQL.
- Purpose-built operations tools: erp_sales_today, erp_collections_today,
  erp_production_today, erp_item_cost, erp_daily_pulse.
- erp_item_cost returns all three COGS definitions with a variance percentage.

**Security hardening** (pre-release)
- The desk page sanitises the LLM-composed final answer before rendering it, since
  markdown conversion does not strip embedded HTML and the answer can quote web
  content or ERP field values verbatim.
- Operational tools that drop to hand-written SQL for joins (sales by item group,
  by brand, by sales person; counter payments; journal entries against receivable;
  production and scrap) now scope every query to the document names the calling
  user is actually permitted to see, the same as erp_fetch_records already did.
  Previously these only checked doctype-level read permission, which did not
  enforce a User Permission restriction such as Territory or Cost Center.
- erp_read_query now refuses to run for any user with a User Permission record,
  since a hand-written query cannot have that row-level restriction verified
  against it generically. Such users are pointed at erp_fetch_records and
  erp_run_report instead.
- MCP Server custom header values are now stored encrypted (Password field),
  matching the Auth Token field, instead of in cleartext.
- Anthropic now has the same three model slots (planner, worker, reflector) as
  OpenAI. Previously one model field served all three roles, contradicting the
  documented design and making it impossible to put a cheap model on the loop.

**Write path**
- Propose, approve, execute through Agent Action Request. The agent never writes directly.
- Auto-approve with a DocType allowlist and a value ceiling. Submit and Cancel excluded.
- Hard-coded blocklist for privilege-granting doctypes.

**Research**
- Market Research Report: submittable, naming series, linked to Item, Brand, Item Group,
  Territory, Customer Group, Supplier Group.
- Findings require a source URL. Confidence and source quality scores derived on save.
- Appears on the Item, Brand and Item Group dashboards.

**Other**
- Tavily search and extract with preferred-domain source scoring.
- Sandboxed pandas analysis via Frappe safe_exec.
- Chart, table, metric and dashboard artifacts rendered with frappe-charts.
- MCP client for external servers, MCP server exposing ERP tools.
- Desk page with a live trace rail.
- Fifteen-case eval harness with hard-fail forbidden-tool checks.
