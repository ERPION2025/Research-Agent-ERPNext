#!/usr/bin/env python3
"""Generate the DocType JSON files for the Research Agent app.

Frappe stores DocTypes as JSON in the repo. Hand-writing them is noisy and
easy to get wrong, so they are declared compactly here and emitted. Re-run
this after changing a field, then `bench --site <site> migrate`.

    python scripts/generate_doctypes.py
"""

from __future__ import annotations

import json
import os
import textwrap

ROOT = os.path.join(os.path.dirname(__file__), "..", "research_agent", "research_agent", "doctype")
MODULE = "Research Agent"


def f(fieldname, label, fieldtype="Data", **kw):
    d = {"fieldname": fieldname, "label": label, "fieldtype": fieldtype}
    d.update(kw)
    return d


def sb(name, label=""):
    return {"fieldname": name, "fieldtype": "Section Break", "label": label}


def cb(name):
    return {"fieldname": name, "fieldtype": "Column Break"}


DOCTYPES = {
    # ------------------------------------------------------------------ settings
    "Research Agent Settings": {
        "issingle": 1,
        "fields": [
            sb("sb_general", "General"),
            f("enabled", "Enable Research Agent", "Check", default="1"),
            f("default_provider", "Default LLM Provider", "Select", options="OpenAI\nAnthropic", default="OpenAI", reqd=1),
            cb("cb_general"),
            f("daily_session_limit", "Daily Runs per User", "Int", default="20",
              description="0 means unlimited."),
            f("retain_sessions_days", "Delete Sessions After (Days)", "Int", default="90"),

            sb("sb_openai", "OpenAI"),
            f("openai_api_key", "OpenAI API Key", "Password"),
            f("openai_planner_model", "Planner Model", "Data", default="o4-mini",
              description="Reasoning model. Decides the plan."),
            f("openai_worker_model", "Worker Model", "Data", default="gpt-4.1-mini",
              description="Fast model. Runs the tool loop and writes the draft."),
            f("openai_reflector_model", "Reflector Model", "Data", default="gpt-4.1",
              description="Strongest model you have. Judges and reflects."),
            cb("cb_openai"),
            f("test_openai", "Test OpenAI", "Button"),

            sb("sb_anthropic", "Anthropic"),
            f("anthropic_api_key", "Anthropic API Key", "Password"),
            f("anthropic_planner_model", "Planner Model", "Data", default="claude-sonnet-5",
              description="Decides the plan."),
            f("anthropic_worker_model", "Worker Model", "Data", default="claude-haiku-4-5-20251001",
              description="Fast model. Runs the tool loop and writes the draft."),
            f("anthropic_reflector_model", "Reflector Model", "Data", default="claude-opus-5",
              description="Strongest model you have. Judges and reflects."),
            cb("cb_anthropic"),
            f("test_anthropic", "Test Anthropic", "Button"),

            sb("sb_tavily", "Web Research (Tavily)"),
            f("allow_web_search", "Allow Web Research", "Check", default="1"),
            f("tavily_api_key", "Tavily API Key", "Password", depends_on="allow_web_search"),
            f("tavily_search_depth", "Search Depth", "Select", options="basic\nadvanced", default="basic",
              depends_on="allow_web_search"),
            f("tavily_max_results", "Max Results per Search", "Int", default="5", depends_on="allow_web_search"),
            cb("cb_tavily"),
            f("preferred_domains", "Preferred Domains", "Small Text", depends_on="allow_web_search",
              description="Comma separated, added to the built-in list. Used to score source quality."),
            f("test_tavily", "Test Tavily", "Button", depends_on="allow_web_search"),

            sb("sb_loop", "Reflexion Loop"),
            f("max_trials", "Max Trials", "Int", default="2",
              description="How many times the agent may re-plan and retry after a failed evaluation."),
            f("max_tool_calls", "Max Tool Calls per Trial", "Int", default="18"),
            f("pass_threshold", "Pass Threshold", "Float", default="0.75",
              description="Weighted evaluator score, 0 to 1, needed to stop early."),
            cb("cb_loop"),
            f("use_llm_judge", "Use LLM Judge", "Check", default="1",
              description="Adds one model call per trial for completeness and usefulness scoring."),
            f("log_prompts", "Log Full Prompts", "Check", default="0",
              description="Verbose. Turn on only while debugging."),

            sb("sb_data", "Data Access"),
            f("max_rows_per_query", "Max Rows per Query", "Int", default="500"),
            f("allow_raw_sql", "Allow Read-Only SQL", "Check", default="0",
              description="Lets the agent write SELECT queries for joins. Every table is still "
                          "doctype-permission-checked, but a hand-written query cannot have a User "
                          "Permission restriction (Territory, Cost Center) verified against it, so "
                          "any user with such a restriction is refused this tool outright."),
            cb("cb_data"),
            f("allowed_doctypes", "DocType Allowlist", "Table MultiSelect",
              options="Research Agent Allowed DocType",
              description="Leave empty to allow every DocType the user already has read permission on. "
                          "Add rows to narrow the agent to a specific surface."),

            sb("sb_write", "Write Actions"),
            f("allow_write_actions", "Allow Write Actions", "Check", default="0",
              description="Lets the agent PROPOSE changes. It can never write directly. Every "
                          "proposal becomes an Agent Action Request that a human approves."),
            f("approver_role", "Approver Role", "Link", options="Role", default="System Manager",
              depends_on="allow_write_actions"),
            f("writable_doctypes", "Writable DocTypes", "Table MultiSelect",
              options="Research Agent Writable DocType", depends_on="allow_write_actions",
              description="Leave empty to allow any DocType the user can already write. "
                          "Recommended: name them explicitly."),
            f("write_blocklist", "Write Blocklist", "Small Text", depends_on="allow_write_actions",
              description="Comma separated. Added to the built-in hard blocklist."),
            cb("cb_write"),
            f("enable_auto_approve", "Enable Auto-Approve", "Check", default="0",
              depends_on="allow_write_actions",
              description="Executes low-risk proposals without a human click. Submit, Cancel and "
                          "Delete are never auto-approved."),
            f("auto_approve_doctypes", "Auto-Approve DocTypes", "Table MultiSelect",
              options="Research Agent Auto Approve DocType", depends_on="enable_auto_approve"),
            f("allow_self_approval", "Allow Self Approval", "Check", default="0",
              depends_on="allow_write_actions",
              description="Off means the person who asked cannot approve their own action. "
                          "Leave off unless you are a single-user site."),
            f("action_expiry_hours", "Expire Pending Actions After (Hours)", "Int", default="72",
              depends_on="allow_write_actions"),
            f("auto_approve_value_limit", "Auto-Approve Value Ceiling", "Currency",
              depends_on="enable_auto_approve",
              description="Proposals above this value always need a human. 0 means no ceiling, "
                          "which is not recommended."),

            sb("sb_mcp", "MCP"),
            f("enable_mcp_client", "Use External MCP Servers", "Check", default="1",
              description="Lets the agent call tools from servers registered under MCP Server."),
            cb("cb_mcp"),
            f("enable_mcp_server", "Expose ERPNext as an MCP Server", "Check", default="0",
              description="Serves this app's ERPNext tools at "
                          "/api/method/research_agent.agent.mcp.server.handle so Claude Desktop and "
                          "other MCP clients can query the ERP. Authenticate with a Frappe API key."),

            sb("sb_diag", "Diagnostics"),
            f("show_versions", "Check Compatibility", "Button"),
            f("version_info", "Detected Versions", "Code", options="JSON", read_only=1),
        ],
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
    },

    # ------------------------------------------------------------- child: allowlist
    "Research Agent Allowed DocType": {
        "istable": 1,
        "fields": [
            f("document_type", "DocType", "Link", options="DocType", in_list_view=1, reqd=1),
        ],
    },

    "Research Agent Writable DocType": {
        "istable": 1,
        "fields": [f("document_type", "DocType", "Link", options="DocType", in_list_view=1, reqd=1)],
    },

    "Research Agent Auto Approve DocType": {
        "istable": 1,
        "fields": [f("document_type", "DocType", "Link", options="DocType", in_list_view=1, reqd=1)],
    },

    # ------------------------------------------------------------------ session
    "Research Session": {
        "autoname": "format:RS-{YYYY}-{#####}",
        "title_field": "title",
        "track_changes": 0,
        "fields": [
            f("title", "Title", "Data", in_list_view=1, read_only=1),
            f("status", "Status", "Select",
              options="Queued\nPlanning\nResearching\nDrafting\nEvaluating\nReflecting\nCompleted\nFailed\nCancelled",
              default="Queued", in_list_view=1, read_only=1),
            cb("cb_head"),
            f("eval_score", "Evaluation Score", "Float", precision="3", read_only=1, in_list_view=1),
            f("current_trial", "Current Trial", "Int", read_only=1),
            f("trials_used", "Trials Used", "Int", read_only=1),

            sb("sb_prompt", "Question"),
            f("prompt", "Prompt", "Small Text", reqd=1),

            sb("sb_answer", "Answer"),
            f("final_answer", "Final Answer", "Markdown Editor", read_only=1),

            sb("sb_artifacts", "Artifacts"),
            f("artifacts", "Artifacts", "Table", options="Research Artifact", read_only=1),

            sb("sb_trace", "Trace", ),
            f("steps", "Steps", "Table", options="Research Step", read_only=1),
            f("reflections", "Reflections", "Long Text", read_only=1),
            f("eval_detail", "Evaluation Detail", "Code", options="JSON", read_only=1),

            sb("sb_outputs", "Published Outputs"),
            f("research_report", "Market Research Report", "Link", options="Market Research Report", read_only=1),
            f("actions_raised", "Action Requests Raised", "Int", read_only=1),

            sb("sb_meta", "Run Metadata"),
            f("input_tokens", "Input Tokens", "Int", read_only=1),
            f("output_tokens", "Output Tokens", "Int", read_only=1),
            cb("cb_meta"),
            f("duration_seconds", "Duration (s)", "Float", precision="1", read_only=1),
            f("error_log", "Error Log", "Code", read_only=1, depends_on="eval:doc.status=='Failed'"),
        ],
        "permissions": [
            {"role": "All", "read": 1, "write": 1, "create": 1, "delete": 1, "if_owner": 1},
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1},
        ],
    },

    # -------------------------------------------------------------- child: step
    "Research Step": {
        "istable": 1,
        "fields": [
            f("trial", "Trial", "Int", in_list_view=1, columns=1),
            f("step_type", "Type", "Select",
              options="Plan\nTool Call\nNote\nDraft\nEvaluation\nReflection",
              in_list_view=1, columns=1),
            f("tool_name", "Tool", "Data", in_list_view=1, columns=2),
            f("duration_ms", "ms", "Int", in_list_view=1, columns=1),
            f("timestamp", "Timestamp", "Datetime"),
            f("content", "Content", "Long Text"),
            f("arguments", "Arguments", "Code", options="JSON"),
            f("output", "Output", "Code", options="JSON"),
        ],
    },

    # ---------------------------------------------------------- child: artifact
    "Research Artifact": {
        "istable": 1,
        "fields": [
            f("artifact_id", "ID", "Data", in_list_view=1, columns=2),
            f("artifact_type", "Type", "Select", options="chart\ntable\nmetric\ndashboard",
              in_list_view=1, columns=2),
            f("title", "Title", "Data", in_list_view=1, columns=5),
            f("spec", "Spec", "Code", options="JSON"),
        ],
    },


    # ------------------------------------------------- market research report
    "Market Research Report": {
        "autoname": "naming_series:",
        "title_field": "subject",
        "is_submittable": 1,
        "track_changes": 1,
        "fields": [
            f("naming_series", "Series", "Select", options="MRR-.YYYY.-", default="MRR-.YYYY.-",
              reqd=1, no_copy=1, print_hide=1),
            f("subject", "Subject", "Data", reqd=1, in_list_view=1,
              description="One specific line. Not 'Market research'."),
            f("research_type", "Research Type", "Select",
              options="Market Sizing\nCompetitor\nPricing\nRegulatory\nCustomer\nSupplier\nTechnology\nDemand\nOther",
              reqd=1, in_list_view=1, in_standard_filter=1),
            cb("cb_mrr_head"),
            f("company", "Company", "Link", options="Company"),
            f("research_date", "Research Date", "Date", reqd=1, in_list_view=1, in_standard_filter=1),
            f("period_from", "Period From", "Date"),
            f("period_to", "Period To", "Date"),
            f("amended_from", "Amended From", "Link", options="Market Research Report",
              read_only=1, no_copy=1, print_hide=1),

            sb("sb_mrr_scope", "Scope"),
            f("item_code", "Item", "Link", options="Item", in_standard_filter=1,
              description="Set when the research is about one specific model."),
            f("item_group", "Item Group", "Link", options="Item Group", in_standard_filter=1),
            f("brand", "Brand", "Link", options="Brand"),
            cb("cb_mrr_scope"),
            f("territory", "Territory", "Link", options="Territory"),
            f("customer_group", "Customer Group", "Link", options="Customer Group"),
            f("supplier_group", "Supplier Group", "Link", options="Supplier Group"),

            sb("sb_mrr_summary", "Executive Summary"),
            f("executive_summary", "Executive Summary", "Markdown Editor"),

            sb("sb_mrr_findings", "Key Findings"),
            f("key_findings", "Key Findings", "Table", options="Research Finding"),

            sb("sb_mrr_comp", "Competitor Observations"),
            f("competitor_observations", "Competitor Observations", "Table",
              options="Competitor Observation"),

            sb("sb_mrr_reco", "Recommendations"),
            f("recommendations", "Recommendations", "Table", options="Research Recommendation"),

            sb("sb_mrr_sources", "Sources"),
            f("sources", "Sources", "Table", options="Research Source"),

            sb("sb_mrr_quality", "Quality"),
            f("confidence_score", "Confidence Score", "Float", precision="3", read_only=1,
              description="Mean confidence across findings."),
            f("source_quality_score", "Source Quality Score", "Float", precision="3", read_only=1,
              description="Share of sources from preferred domains."),
            cb("cb_mrr_quality"),
            f("research_session", "Research Session", "Link", options="Research Session", read_only=1),
            f("originating_prompt", "Originating Prompt", "Small Text", read_only=1),
            f("reviewed_by", "Reviewed By", "Link", options="User"),
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1,
             "submit": 1, "cancel": 1, "amend": 1, "report": 1, "export": 1, "share": 1, "print": 1},
            {"role": "Sales Manager", "read": 1, "write": 1, "create": 1, "submit": 1,
             "report": 1, "export": 1, "print": 1},
            {"role": "Purchase Manager", "read": 1, "write": 1, "create": 1, "submit": 1,
             "report": 1, "print": 1},
            {"role": "Item Manager", "read": 1, "write": 1, "create": 1, "report": 1},
            {"role": "Accounts Manager", "read": 1, "report": 1, "print": 1},
            {"role": "Stock Manager", "read": 1, "report": 1},
        ],
    },

    "Research Finding": {
        "istable": 1,
        "fields": [
            f("finding", "Finding", "Small Text", in_list_view=1, columns=5, reqd=1),
            f("category", "Category", "Select",
              options="Market\nPricing\nCompetitor\nRegulatory\nDemand\nSupply\nRisk\nOther",
              default="Other", in_list_view=1, columns=1),
            f("impact", "Impact", "Select", options="High\nMedium\nLow", default="Medium",
              in_list_view=1, columns=1),
            f("confidence", "Confidence", "Float", precision="2", in_list_view=1, columns=1),
            f("evidence", "Evidence", "Small Text",
              description="The number, quote or figure this rests on."),
            f("source_url", "Source URL", "Data", options="URL", reqd=1, in_list_view=1, columns=3),
        ],
    },

    "Research Source": {
        "istable": 1,
        "fields": [
            f("url", "URL", "Data", options="URL", in_list_view=1, columns=4, reqd=1),
            f("title", "Title", "Data", in_list_view=1, columns=3),
            f("domain", "Domain", "Data", in_list_view=1, columns=2, read_only=1),
            f("is_preferred", "Preferred", "Check", in_list_view=1, columns=1, read_only=1),
            f("published_date", "Published", "Data"),
            f("snippet", "Snippet", "Small Text"),
        ],
    },

    "Competitor Observation": {
        "istable": 1,
        "fields": [
            f("competitor_name", "Competitor", "Data", in_list_view=1, columns=2, reqd=1),
            f("model_or_product", "Model or Product", "Data", in_list_view=1, columns=3),
            f("observed_price", "Observed Price", "Float", precision="2", in_list_view=1, columns=2),
            f("currency", "Currency", "Link", options="Currency", in_list_view=1, columns=1),
            f("observed_on", "Observed On", "Date"),
            f("positioning", "Positioning", "Small Text"),
            f("source_url", "Source URL", "Data", options="URL", in_list_view=1, columns=3),
        ],
    },

    "Research Recommendation": {
        "istable": 1,
        "fields": [
            f("recommendation", "Recommendation", "Small Text", in_list_view=1, columns=5, reqd=1),
            f("priority", "Priority", "Select", options="High\nMedium\nLow", default="Medium",
              in_list_view=1, columns=1),
            f("owner_role", "Owner Function", "Data", in_list_view=1, columns=2),
            f("expected_impact", "Expected Impact", "Small Text", in_list_view=1, columns=3),
        ],
    },

    # ---------------------------------------------------- agent action request
    "Agent Action Request": {
        "autoname": "naming_series:",
        "title_field": "target_doctype",
        "track_changes": 1,
        "fields": [
            f("naming_series", "Series", "Select", options="AAR-.YYYY.-", default="AAR-.YYYY.-",
              reqd=1, no_copy=1, print_hide=1),
            f("status", "Status", "Select",
              options="Pending Approval\nApproved\nRejected\nExecuted\nFailed\nExpired",
              default="Pending Approval", in_list_view=1, in_standard_filter=1, read_only=1),
            f("action_type", "Action", "Select", options="Create\nUpdate\nSubmit\nCancel",
              reqd=1, in_list_view=1, read_only=1),
            cb("cb_aar_head"),
            f("target_doctype", "Target DocType", "Link", options="DocType", reqd=1,
              in_list_view=1, read_only=1),
            f("target_docname", "Target Document", "Dynamic Link", options="target_doctype",
              in_list_view=1, read_only=1),
            f("risk_level", "Risk", "Select", options="Low\nMedium\nHigh", read_only=1,
              in_list_view=1, in_standard_filter=1),
            f("estimated_value", "Estimated Value", "Currency", read_only=1, in_list_view=1),

            sb("sb_aar_why", "Why"),
            f("reason", "Reason", "Small Text", read_only=1),
            f("originating_prompt", "Originating Prompt", "Small Text", read_only=1),
            f("research_session", "Research Session", "Link", options="Research Session", read_only=1),

            sb("sb_aar_what", "What Will Change"),
            f("payload", "Payload", "Code", options="JSON", read_only=1),
            f("diff_preview", "Diff Preview", "HTML", read_only=1),

            sb("sb_aar_outcome", "Outcome"),
            f("approved_by", "Approved By", "Link", options="User", read_only=1),
            f("approved_on", "Approved On", "Datetime", read_only=1),
            f("approval_mode", "Approval Mode", "Select", options="\nManual\nAuto", read_only=1),
            f("auto_approve_note", "Auto-Approve Note", "Small Text", read_only=1),
            cb("cb_aar_outcome"),
            f("result_docname", "Resulting Document", "Dynamic Link", options="target_doctype", read_only=1),
            f("rejection_reason", "Rejection Reason", "Small Text", read_only=1),
            f("error_log", "Error Log", "Code", read_only=1,
              depends_on="eval:doc.status=='Failed'"),
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1,
             "report": 1, "export": 1},
            {"role": "All", "read": 1, "create": 1, "if_owner": 1},
        ],
    },

    # ------------------------------------------------------------- tool registry
    "Agent Tool": {
        "autoname": "field:tool_name",
        "fields": [
            f("tool_name", "Tool Name", "Data", reqd=1, unique=1, in_list_view=1),
            f("enabled", "Enabled", "Check", default="1", in_list_view=1),
            f("tool_type", "Type", "Select", options="Builtin\nMCP", default="Builtin", in_list_view=1, read_only=1),
            cb("cb_tool"),
            f("category", "Category", "Data", read_only=1, in_list_view=1),
            f("mcp_server", "MCP Server", "Link", options="MCP Server", read_only=1,
              depends_on="eval:doc.tool_type=='MCP'"),
            f("is_builtin", "Is Builtin", "Check", read_only=1, hidden=1),

            sb("sb_tool_detail", "Detail"),
            f("description", "Description", "Small Text", read_only=1),
            f("allowed_roles", "Restrict to Roles", "Small Text",
              description="Comma separated role names. Leave empty to allow every user who already has "
                          "permission on the underlying data."),
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "All", "read": 1},
        ],
    },

    # ----------------------------------------------------------------- mcp server
    "MCP Server": {
        "autoname": "field:server_name",
        "fields": [
            f("server_name", "Server Name", "Data", reqd=1, unique=1, in_list_view=1),
            f("enabled", "Enabled", "Check", default="1", in_list_view=1),
            cb("cb_mcp_head"),
            f("tool_count", "Tools", "Int", read_only=1, in_list_view=1),
            f("last_synced", "Last Synced", "Datetime", read_only=1),

            sb("sb_conn", "Connection"),
            f("url", "Server URL", "Data", reqd=1,
              description="Streamable HTTP endpoint. For stdio servers, front them with mcp-proxy."),
            f("timeout", "Timeout (s)", "Int", default="60"),
            cb("cb_conn"),
            f("auth_scheme", "Auth Scheme", "Select", options="Bearer\ntoken\nBasic\n", default="Bearer"),
            f("auth_token", "Auth Token", "Password"),

            sb("sb_headers", "Custom Headers"),
            f("custom_headers", "Custom Headers", "Table", options="MCP Header"),

            sb("sb_tools", "Discovered Tools"),
            f("tool_prefix", "Tool Prefix", "Data",
              description="Namespaces this server's tools, e.g. prefix 'wh' gives 'wh__query'. "
                          "Defaults to a scrubbed server name."),
            f("refresh_tools", "Refresh Tools", "Button"),
            f("cached_tools", "Cached Tools", "Code", options="JSON", read_only=1),
            f("server_info", "Server Info", "Code", options="JSON", read_only=1),
            f("last_error", "Last Error", "Small Text", read_only=1),
        ],
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
    },

    "MCP Header": {
        "istable": 1,
        "fields": [
            f("header_key", "Key", "Data", in_list_view=1, reqd=1),
            f("header_value", "Value", "Password",
              description="Encrypted at rest, the same as the Auth Token field above."),
        ],
    },
}


BASE = {
    "actions": [],
    "creation": "2026-01-01 00:00:00.000000",
    "doctype": "DocType",
    "engine": "InnoDB",
    "index_web_pages_for_search": 0,
    "links": [],
    "modified": "2026-01-01 00:00:00.000000",
    "modified_by": "Administrator",
    "module": MODULE,
    "owner": "Administrator",
    "sort_field": "modified",
    "sort_order": "DESC",
    "states": [],
}


def build(name: str, spec: dict) -> dict:
    doc = dict(BASE)
    doc["name"] = name
    doc["field_order"] = [f["fieldname"] for f in spec["fields"]]
    doc["fields"] = spec["fields"]
    doc["permissions"] = spec.get("permissions", [])
    for key in ("issingle", "istable", "autoname", "title_field", "track_changes",
                "is_submittable", "naming_rule"):
        if key in spec:
            doc[key] = spec[key]
    if spec.get("istable"):
        doc["editable_grid"] = 1
    return doc


def scrub(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


def main():
    for name, spec in DOCTYPES.items():
        folder = os.path.join(ROOT, scrub(name))
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, "__init__.py"), "a").close()

        with open(os.path.join(folder, f"{scrub(name)}.json"), "w") as fh:
            json.dump(build(name, spec), fh, indent=1)
            fh.write("\n")

        controller = os.path.join(folder, f"{scrub(name)}.py")
        if not os.path.exists(controller):
            cls = name.replace(" ", "")
            base = "Document"
            with open(controller, "w") as fh:
                fh.write(
                    textwrap.dedent(
                        f'''\
                        # Copyright (c) 2026, ERPion Technologies LLP and contributors
                        # For license information, please see license.txt

                        from frappe.model.document import {base}


                        class {cls}({base}):
                        \tpass
                        '''
                    )
                )
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
