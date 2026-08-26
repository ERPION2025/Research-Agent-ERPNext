import frappe


def after_install():
    sync_builtin_tools()
    seed_defaults()
    frappe.db.commit()


def sync_builtin_tools():
    """Create an Agent Tool row per builtin tool. Idempotent, runs on migrate."""
    from research_agent.agent.registry import sync_registry

    added = sync_registry()
    if added:
        print(f"Research Agent: registered {added} new tools")


def seed_defaults():
    settings = frappe.get_single("Research Agent Settings")
    if settings.get("max_trials"):
        return
    settings.update(
        {
            "enabled": 0,  # off until an API key is entered
            "default_provider": "OpenAI",
            "openai_planner_model": "o4-mini",
            "openai_worker_model": "gpt-4.1-mini",
            "openai_reflector_model": "gpt-4.1",
            "anthropic_planner_model": "claude-sonnet-5",
            "anthropic_worker_model": "claude-haiku-4-5-20251001",
            "anthropic_reflector_model": "claude-opus-5",
            "max_trials": 2,
            "max_tool_calls": 18,
            "pass_threshold": 0.75,
            "use_llm_judge": 1,
            "max_rows_per_query": 500,
            "allow_raw_sql": 0,
            "allow_web_search": 1,
            "tavily_search_depth": "basic",
            "tavily_max_results": 5,
            "daily_session_limit": 20,
            "retain_sessions_days": 90,
            "enable_mcp_client": 1,
            "enable_mcp_server": 0,
            # write path ships off. Turning it on is a decision someone makes
            # deliberately, not a default they inherit.
            "allow_write_actions": 0,
            "approver_role": "System Manager",
            "enable_auto_approve": 0,
            "allow_self_approval": 0,
            "action_expiry_hours": 72,
            "auto_approve_value_limit": 0,
        }
    )
    settings.save(ignore_permissions=True)
    print("Research Agent: settings seeded. Add your API keys, then set Enabled.")


def before_uninstall():
    for dt in ("Research Session", "Agent Tool", "MCP Server"):
        frappe.db.delete(dt)
    frappe.db.commit()
