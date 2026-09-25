// Copyright (c) 2026, ERPion Technologies LLP and contributors

frappe.ui.form.on("Research Agent Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Open Research Agent"), () => frappe.set_route("research-agent-workbench"));
		if (frm.doc.enable_knowledge_base) {
			frappe.call("research_agent.agent.rag.ingest.index_summary").then((r) => {
				const d = r.message || {};
				const by = {};
				(d.by_status || []).forEach((x) => (by[x.status] = x));
				const failed = (by.Failed || {}).count || 0;
				const indexed = (by.Indexed || {}).count || 0;
				const flagged = d.arithmetic_flagged || 0;
				frm.dashboard.set_headline_alert(
					__("{0} files indexed, {1} chunks, {2} pages OCR'd, ${3} spent. {4}{5}", [
						indexed,
						d.total_chunks || 0,
						d.pages_ocred || 0,
						(d.total_cost || 0).toFixed(2),
						failed ? __("{0} failed. ", [failed]) : "",
						flagged ? __("{0} document(s) do not add up internally.", [flagged]) : "",
					]),
					failed || flagged ? "orange" : "green"
				);
				if (flagged) {
					frm.add_custom_button(__("Documents that do not reconcile"), () =>
						frappe.set_route("List", "Document Index Status", { arithmetic_flags: ["is", "set"] })
					);
				}
			});
			frm.add_custom_button(__("Indexing status"), () =>
				frappe.set_route("List", "Document Index Status")
			);
		}

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

	reindex_now(frm) {
		frappe.confirm(
			__(
				"This queues every eligible file for indexing. Scanned pages go through a vision " +
					"model, which costs money. Files that have not changed are skipped. Continue?"
			),
			() =>
				frm.save().then(() =>
					frappe.call({
						method: "research_agent.agent.rag.ingest.reindex_all",
						freeze: true,
						freeze_message: __("Queueing files..."),
						callback: (r) => {
							frappe.msgprint({
								title: __("{0} files queued", [r.message.queued]),
								message: r.message.note,
								indicator: "blue",
							});
						},
					})
				)
		);
	},

	enable_knowledge_base(frm) {
		if (frm.doc.enable_knowledge_base) {
			frappe.msgprint({
				title: __("Before you turn this on"),
				indicator: "orange",
				message: __(
					"Document text is sent to OpenAI at index time, including contracts. " +
						"Nothing indexes until you press Reindex. Name the DocTypes to index rather " +
						"than leaving the list empty, and leave 'Index Unattached Files' off unless " +
						"every Drive file is safe for every user."
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
