// Copyright (c) 2026, ERPion Technologies LLP and contributors

frappe.ui.form.on("MCP Server", {
	refresh(frm) {
		if (frm.doc.last_error) {
			frm.dashboard.set_headline_alert(
				__("Last sync failed: {0}", [frappe.utils.escape_html(frm.doc.last_error)]),
				"red"
			);
		} else if (frm.doc.tool_count) {
			frm.dashboard.set_headline_alert(
				__("{0} tools discovered, last synced {1}", [
					frm.doc.tool_count,
					frappe.datetime.comment_when(frm.doc.last_synced),
				]),
				"green"
			);
		}
	},

	refresh_tools(frm) {
		frm.save().then(() => {
			frappe.call({
				method: "research_agent.agent.mcp.client.refresh_server",
				args: { server: frm.doc.name },
				freeze: true,
				freeze_message: __("Talking to the MCP server..."),
				callback: () => {
					frappe.show_alert({ message: __("Tools refreshed"), indicator: "green" });
					frm.reload_doc();
				},
			});
		});
	},
});
