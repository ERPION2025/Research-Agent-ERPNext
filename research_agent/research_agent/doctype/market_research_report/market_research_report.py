# Copyright (c) 2026, ERPion Technologies LLP and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MarketResearchReport(Document):
	def validate(self):
		self.validate_period()
		self.validate_findings()
		self.recompute_scores()

	def validate_period(self):
		if self.period_from and self.period_to and self.period_from > self.period_to:
			frappe.throw(_("Period From is after Period To."))

	def validate_findings(self):
		if not self.key_findings:
			frappe.throw(_("A research report needs at least one finding."))
		for row in self.key_findings:
			if not (row.source_url or "").strip():
				frappe.throw(_("Finding {0} has no source URL.").format(row.idx))
			if row.confidence and not 0 <= row.confidence <= 1:
				frappe.throw(_("Confidence on finding {0} must be between 0 and 1.").format(row.idx))

	def recompute_scores(self):
		"""Scores are derived, not typed. Recompute on every save so an edited
		report cannot keep a stale quality number."""
		confidences = [r.confidence for r in self.key_findings if r.confidence]
		self.confidence_score = round(sum(confidences) / len(confidences), 3) if confidences else 0

		if self.sources:
			preferred = sum(1 for s in self.sources if s.is_preferred)
			self.source_quality_score = round(preferred / len(self.sources), 3)
		else:
			self.source_quality_score = 0

	def on_submit(self):
		self.reviewed_by = self.reviewed_by or frappe.session.user
		if self.research_session:
			frappe.db.set_value("Research Session", self.research_session, "research_report", self.name)


def get_dashboard_data(data=None):
	return {
		"fieldname": "item_code",
		"transactions": [{"label": _("Research"), "items": ["Market Research Report"]}],
	}
