// Copyright (c) 2026, ERPion Technologies LLP and contributors

frappe.ui.form.on("Research Agent Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Open Research Agent"), () => frappe.set_route("research-agent"));
		frm.add_custom_button(__("Show My Tools"), () => {
			frappe.call("research_agent.api.list_tools").then((r) => {
				const d = r.message;
				const html = Object.entries(d.tools)
					.map(
						([cat, tools]) =>
							`<h5>${cat}</h5><ul>${tools
								.map((t) => `<li><code>${t.name}</code> - ${frappe.utils.escape_html(t.description)}</li>`)
								.join("")}</ul>`
					)
					.join("");
				frappe.msgprint({
					title: __("{0} tools available to {1}", [d.count, d.user]),
					message: html,
					wide: true,
				});
			});
		});
	},

	show_versions(frm) {
		frappe.call({ method: "research_agent.api.compatibility", freeze: true }).then((r) => {
			frm.set_value("version_info", JSON.stringify(r.message, null, 2));
			frappe.msgprint({
				title: __("Detected versions"),
				message: `<pre>${JSON.stringify(r.message, null, 2)}</pre>`,
			});
		});
	},

	allow_write_actions(frm) {
		if (frm.doc.allow_write_actions) {
			frappe.msgprint({
				title: __("Before you turn this on"),
				indicator: "orange",
				message: __(
					"The agent still cannot write directly. It can only raise Agent Action Requests " +
						"that someone approves. Name the writable DocTypes explicitly rather than " +
						"leaving the list empty, and keep auto-approve off until you have watched " +
						"a few requests come through."
				),
			});
		}
	},

	test_openai: (frm) => test(frm, "OpenAI"),
	test_anthropic: (frm) => test(frm, "Anthropic"),
	test_tavily: (frm) => test(frm, "Tavily"),
});

function test(frm, provider) {
	frm.save().then(() => {
		frappe.call({
			method: "research_agent.api.test_credentials",
			args: { provider },
			freeze: true,
			freeze_message: __("Calling {0}...", [provider]),
			callback: (r) => {
				frappe.show_alert({
					message: __("{0} is working. {1}", [provider, JSON.stringify(r.message)]),
					indicator: "green",
				});
			},
		});
	});
}
