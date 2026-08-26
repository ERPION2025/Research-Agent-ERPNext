import frappe


def execute():
	# The Page used to be named "research-agent", same route as the "Research
	# Agent" Workspace. Renaming the fixture file to research-agent-workbench
	# leaves the old Page row in place on any site that already migrated, so
	# it has to be deleted explicitly or the route collision comes right back.
	if frappe.db.exists("Page", "research-agent"):
		frappe.delete_doc("Page", "research-agent", ignore_permissions=True, force=True)
