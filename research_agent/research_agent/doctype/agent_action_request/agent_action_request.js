// Copyright (c) 2026, ERPion Technologies LLP and contributors

frappe.ui.form.on("Agent Action Request", {
	refresh(frm) {
		const colour = { High: "red", Medium: "orange", Low: "blue" }[frm.doc.risk_level] || "gray";
		frm.dashboard.set_headline_alert(
			__("{0} {1}. Risk {2}.", [frm.doc.action_type, frm.doc.target_doctype, frm.doc.risk_level]),
			colour
		);

		if (frm.doc.status === "Pending Approval") {
			frm.page.set_primary_action(__("Approve and run"), () => {
				frappe.confirm(
					__("This will {0} {1} as {2}. Continue?", [
						frm.doc.action_type.toLowerCase(),
						frm.doc.target_doctype,
						frm.doc.owner,
					]),
					() =>
						frm.call("approve").then((r) => {
							frappe.show_alert({
								message: __("Done: {0}", [r.message.document || r.message.status]),
								indicator: r.message.status === "Executed" ? "green" : "red",
							});
							frm.reload_doc();
						})
				);
			});

			frm.add_custom_button(__("Reject"), () => {
				frappe.prompt(
					{ fieldname: "reason", label: __("Why"), fieldtype: "Small Text", reqd: 1 },
					(v) => frm.call("reject", { reason: v.reason }).then(() => frm.reload_doc()),
					__("Reject this action")
				);
			});
		}

		if (frm.doc.result_docname) {
			frm.add_custom_button(__("Open {0}", [frm.doc.target_doctype]), () =>
				frappe.set_route("Form", frm.doc.target_doctype, frm.doc.result_docname)
			);
		}
	},
});
