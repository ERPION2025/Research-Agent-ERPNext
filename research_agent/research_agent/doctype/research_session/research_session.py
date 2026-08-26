# Copyright (c) 2026, ERPion Technologies LLP and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ResearchSession(Document):
	pass


def purge_old_sessions():
	"""Daily cleanup. Sessions carry full tool outputs, so they grow fast."""
	import frappe

	days = frappe.db.get_single_value("Research Agent Settings", "retain_sessions_days")
	if not days:
		return
	cutoff = frappe.utils.add_days(frappe.utils.today(), -int(days))
	names = frappe.get_all(
		"Research Session", filters={"creation": ["<", cutoff]}, pluck="name", limit_page_length=500
	)
	for name in names:
		frappe.delete_doc("Research Session", name, force=True, ignore_permissions=True)
	frappe.db.commit()
