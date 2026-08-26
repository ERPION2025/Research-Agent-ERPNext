// Copyright (c) 2026, ERPion Technologies LLP and contributors

frappe.ui.form.on("Market Research Report", {
	refresh(frm) {
		if (frm.doc.source_quality_score != null && frm.doc.docstatus === 0) {
			const s = frm.doc.source_quality_score;
			frm.dashboard.set_headline_alert(
				__("Source quality {0}. {1}", [
					(s * 100).toFixed(0) + "%",
					s < 0.4
						? __("Under 40% of sources are from preferred domains. Check before submitting.")
						: __("Sources look reasonable."),
				]),
				s < 0.4 ? "orange" : "green"
			);
		}

		if (frm.doc.research_session) {
			frm.add_custom_button(__("Open Research Session"), () =>
				frappe.set_route("Form", "Research Session", frm.doc.research_session)
			);
		}
		if (frm.doc.docstatus === 1 && frm.doc.item_code) {
			frm.add_custom_button(__("Open Item"), () =>
				frappe.set_route("Form", "Item", frm.doc.item_code)
			);
		}
	},
});
