# Changelog

## 0.3.2 - the keyword half of hybrid search actually runs

- Migration patch adds the MariaDB FULLTEXT index on Document Chunk.chunk_text.
  Without it, keyword_search's MATCH() AGAINST() query fails on every site,
  silently, and retrieval degrades to dense-only forever with no visible error.
  Frappe's doctype JSON has no way to declare this, so it has to be a raw DDL
  patch. Found while writing a production-readiness assessment, not by testing.


## 0.3.0 - RAG layer

**Document search**
- Permission-masked retrieval: ERPNext decides visibility via get_list, then the
  vector matrix is masked to the survivors. No ACL copy on the vectors to drift.
- Second gate re-checks the parent document on every chunk before it enters a prompt.
- Payroll doctypes hard-denied in code, not by setting.
- Relevance floor so an unrelated nearest neighbour is dropped rather than cited.
- Empty results name which of three causes applied: nothing indexed, permission
  boundary, or nothing matched.
- Hybrid dense plus MariaDB FULLTEXT, fused by reciprocal rank.
- pypdfium2 parsing with vision-model OCR only for pages with no text layer.
- Clause-aware chunking with contextual retrieval.
- Embedding dedup by content hash; query and corpus model mismatch throws.

**Citations**
- Model writes [D1] markers; code owns the link. A fabricated marker cannot resolve
  to a working URL because the model never writes one.
- citation_integrity is the seventh evaluator check. Fabricated marker scores zero.
- Unresolved markers shown struck through in the UI rather than hidden.

**Evals**
- evals/retrieval.py with rank metrics (no API calls) and optional Ragas.
- run_permission_check() probes as a restricted user.
- Tests: 21 permission, 21 chunking, 19 citation.


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

## 0.3.1 - document self-consistency

- reconcile.py: two checks over OCR'd text, independent of the ERP.
  tax_reconciliation (taxable + CGST/SGST/IGST vs stated total) and
  itemised_total (numbered line items vs stated total).
- Bug found and fixed during testing: the amount regex truncated any bare
  4+ digit number with no comma (2017 -> 201, 7). Every plain rupee amount
  without Indian comma grouping was silently wrong. Caught by testing
  against real invoices, not by review.
- Wired into ingest.py, runs once per page pre-chunking. Result stored on
  Document Index Status.arithmetic_flags and surfaced through kb_search
  hits as arithmetic_flag, so a quoted total carries its own caveat.
- Settings dashboard shows a flagged-document count with a jump-to-list button.
- Tuned and tested against six real vendor invoices (handwritten labour
  bills and GST tax invoices). One of the six failed its own arithmetic by
  Rs 100; the check catches it, the other five stay silent.
- 21 new tests in test_reconciliation.py.
