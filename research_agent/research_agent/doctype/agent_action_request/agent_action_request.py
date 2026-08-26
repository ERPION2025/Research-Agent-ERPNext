# Copyright (c) 2026, ERPion Technologies LLP and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document


class AgentActionRequest(Document):
	def validate(self):
		self.build_diff_preview()

	def build_diff_preview(self):
		"""The approver reads this, not the JSON. Make it legible."""
		try:
			payload = json.loads(self.payload or "{}")
		except json.JSONDecodeError:
			self.diff_preview = "<p class='text-danger'>Payload is not valid JSON.</p>"
			return

		if self.action_type == "Update":
			diff = payload.get("diff") or {}
			rows = "".join(
				f"<tr><td>{frappe.utils.escape_html(k)}</td>"
				f"<td class='text-muted'>{frappe.utils.escape_html(str(v.get('from')))}</td>"
				f"<td><b>{frappe.utils.escape_html(str(v.get('to')))}</b></td></tr>"
				for k, v in diff.items()
			)
			self.diff_preview = (
				"<table class='table table-sm'><thead><tr><th>Field</th>"
				"<th>Current</th><th>Proposed</th></tr></thead>"
				f"<tbody>{rows}</tbody></table>"
			)
		elif self.action_type == "Create":
			rows = "".join(
				f"<tr><td>{frappe.utils.escape_html(k)}</td>"
				f"<td>{frappe.utils.escape_html(json.dumps(v, default=str)[:300])}</td></tr>"
				for k, v in payload.items()
			)
			self.diff_preview = (
				"<table class='table table-sm'><thead><tr><th>Field</th>"
				f"<th>Value</th></tr></thead><tbody>{rows}</tbody></table>"
			)
		else:
			self.diff_preview = (
				f"<p>{self.action_type} <b>{frappe.utils.escape_html(self.target_doctype)} "
				f"{frappe.utils.escape_html(self.target_docname or '')}</b>. "
				"This posts or reverses ledger entries.</p>"
			)

	def _check_approver(self):
		role = frappe.db.get_single_value("Research Agent Settings", "approver_role") or "System Manager"
		if role not in frappe.get_roles():
			frappe.throw(_("Only users with the {0} role can approve agent actions.").format(role),
			             frappe.PermissionError)
		if self.owner == frappe.session.user and not frappe.db.get_single_value(
			"Research Agent Settings", "allow_self_approval"
		):
			frappe.throw(_("You cannot approve your own request."), frappe.PermissionError)

	@frappe.whitelist()
	def approve(self):
		self._check_approver()
		if self.status != "Pending Approval":
			frappe.throw(_("This request is {0}.").format(self.status))
		from research_agent.agent.tools.write_actions import execute_request

		return execute_request(self.name, approver=frappe.session.user)

	@frappe.whitelist()
	def reject(self, reason: str = ""):
		self._check_approver()
		if self.status != "Pending Approval":
			frappe.throw(_("This request is {0}.").format(self.status))
		self.db_set("status", "Rejected")
		self.db_set("rejection_reason", reason or "")
		self.db_set("approved_by", frappe.session.user)
		self.db_set("approved_on", frappe.utils.now())
		frappe.db.commit()
		return {"status": "Rejected"}


def expire_stale_requests():
	"""Daily. An unapproved action is stale after the configured window; leaving
	them Pending forever means the queue stops being read."""
	hours = frappe.db.get_single_value("Research Agent Settings", "action_expiry_hours") or 72
	cutoff = frappe.utils.add_to_date(frappe.utils.now(), hours=-int(hours))
	names = frappe.get_all(
		"Agent Action Request",
		filters={"status": "Pending Approval", "creation": ["<", cutoff]},
		pluck="name",
	)
	for n in names:
		frappe.db.set_value("Agent Action Request", n, "status", "Expired", update_modified=False)
	frappe.db.commit()
	return len(names)
