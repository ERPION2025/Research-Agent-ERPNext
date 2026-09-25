app_name = "research_agent"
app_title = "Research Agent"
app_publisher = "ERPion Technologies LLP"
app_description = (
    "Reflexion research and analytics agent for ERPNext. Answers business questions "
    "from your own ERP data plus live web research, and returns charts, tables and dashboards."
)
app_email = "pranav@erpion.in"
app_license = "mit"
required_apps = ["frappe/erpnext"]

# Desk assets
app_include_js = ["research_agent.bundle.js"]
app_include_css = ["research_agent.bundle.css"]

# Workspace shown in the sidebar
add_to_apps_screen = [
    {
        "name": "research_agent",
        "logo": "/assets/research_agent/images/logo.svg",
        "title": "Research Agent",
        "route": "/app/research-agent-workbench",
    }
]

# Fixtures shipped with the app: the built-in tool registry rows
fixtures = [
    {
        "dt": "Agent Tool",
        "filters": [["is_builtin", "=", 1]],
    }
]

after_install = "research_agent.install.after_install"
after_migrate = "research_agent.install.sync_builtin_tools"

scheduler_events = {
    "hourly": [
        "research_agent.agent.mcp.client.refresh_all_servers",
    ],
    "daily": [
        "research_agent.agent.rag.ingest.retry_failed",
        "research_agent.research_agent.doctype.research_session.research_session.purge_old_sessions",
        "research_agent.research_agent.doctype.agent_action_request.agent_action_request.expire_stale_requests",
    ],
}

# Market Research Report shows up on the Item, Brand and Item Group dashboards
# so research is found where the product is, not in a separate silo.
override_doctype_dashboards = {
    "Item": "research_agent.dashboards.item_dashboard",
    "Brand": "research_agent.dashboards.brand_dashboard",
    "Item Group": "research_agent.dashboards.item_group_dashboard",
}

doc_events = {
    "File": {
        "after_insert": "research_agent.agent.rag.ingest.on_file_insert",
        "on_trash": "research_agent.agent.rag.ingest.on_file_delete",
    },
    "Market Research Report": {
        "on_submit": "research_agent.dashboards.clear_research_cache",
    }
}

# Permission-sensitive endpoints are all whitelisted in api.py explicitly.
# Nothing here is exposed as a guest method on purpose.
override_whitelisted_methods = {}

# MCP server endpoint is served through api.py, guarded by a bearer token.
