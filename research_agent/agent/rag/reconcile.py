"""Arithmetic reconciliation.

Vendor invoices are handwritten, and handwritten arithmetic is sometimes
wrong before the page is ever scanned. This is a different failure from the
ERP-versus-document disagreement the rest of the knowledge base is built
around: here the document disagrees with itself, its stated total does not
match its own line items. Worth catching for the same reason as everything
else in this app: a total nobody checked is a total somebody might pay.

Two checks, run on the OCR'd or extracted text of one page:

  tax reconciliation   taxable amount + CGST + SGST/IGST should equal the
                        stated grand total, for a standard GST invoice
  itemised total        the numbered line items on a labour or service bill
                        should sum to the stated total

Both are pattern matches over free text rather than a structured parser for
a known layout, because there is no consistent layout. Six vendors in six
towns produce six handwritten formats. These two checks were tuned against
six real sample invoices, not against an assumption of what an invoice
looks like, and the fixtures in the test file are those six documents.

The bar for raising a finding is deliberately high, for the same reason the
rest of the anomaly detection in this app is conservative: a check that
fires on ordinary rounding trains people to stop reading it. A minimum of
two line items is required, a materiality tolerance is applied, and the
wording never asserts the vendor is wrong. An OCR misread of one digit
produces an identical symptom, and only a human looking at the original can
tell the two apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A bare four-or-more-digit number ("2017", "10680") must match whole, not as
# a 3-digit group followed by a stray digit. The alternation tries the
# comma-grouped form first (1,20,000 or 1,200,000, either grouping style),
# then falls back to a plain run of digits of any length. Getting this wrong
# is not cosmetic: it silently truncates every four-digit rupee amount that
# has no comma, which is most handwritten ones, and every downstream sum
# becomes wrong in a way that produces a plausible-looking but false anomaly.
NUMBER = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?")

# Lines carrying these are never treated as a line-item amount, because the
# number on them is a code, a phone number, a date or a rate, not a charge.
SKIP_KEYWORDS = (
    "bill no", "invoice no", "invoice serial", "gstin", "gst no", "hsn", "sac",
    "mob", "mobile", "phone", "cell no", "cell:", "vehicle no", "vehicle number",
    "date", "state code", "ref no", "reference", " km", "km.", "gst%", "gst %",
    "qty", "rate", "tax rate", "sr.", "sr no", "p.o. no", "po no", "account no",
    "ifs code", "branch",
)

TOTAL_RE = re.compile(r"\btotal\b", re.IGNORECASE)
SUBTOTAL_RE = re.compile(
    r"\bsub\s*-?\s*total\b|\bbefore\s+tax\b|\btaxable\s+amount\b", re.IGNORECASE
)
TAX_RE = re.compile(r"\b(CGST|SGST|IGST)\b", re.IGNORECASE)


@dataclass
class ReconciliationFinding:
    check: str
    stated_total: float
    computed_total: float
    difference: float
    severity: str
    detail: str
    suggested_action: str
    components: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "stated_total": self.stated_total,
            "computed_total": self.computed_total,
            "difference": round(self.difference, 2),
            "severity": self.severity,
            "detail": self.detail,
            "suggested_action": self.suggested_action,
            "components": self.components,
        }


def _numbers_in(line: str) -> list[float]:
    out = []
    for m in NUMBER.findall(line):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def _last_number(line: str) -> float | None:
    nums = _numbers_in(line)
    return nums[-1] if nums else None


def _has_skip_keyword(line: str) -> bool:
    lowered = line.lower()
    return any(k in lowered for k in SKIP_KEYWORDS)


def _tolerance(total: float) -> float:
    """Loose enough for paise-level rounding, tight enough to catch a line
    that was missed or mistyped. Half a percent, floored at two rupees."""
    return max(2.0, round(total * 0.005, 2))


def _severity(difference: float, total: float) -> str:
    if total <= 0:
        return "Low"
    return "High" if abs(difference) / total > 0.10 else "Medium"


# ---------------------------------------------------------------------------
# check 1: taxable amount + CGST/SGST/IGST should equal the stated total
# ---------------------------------------------------------------------------
def check_tax_reconciliation(text: str) -> ReconciliationFinding | None:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    subtotal, taxes, total = None, {}, None

    for line in lines:
        if SUBTOTAL_RE.search(line) and not TAX_RE.search(line):
            n = _last_number(line)
            if n is not None:
                subtotal = n
            continue
        tax_match = TAX_RE.search(line)
        if tax_match:
            n = _last_number(line)
            if n is not None:
                taxes[tax_match.group(1).upper()] = n
            continue
        if TOTAL_RE.search(line) and not SUBTOTAL_RE.search(line):
            n = _last_number(line)
            if n is not None:
                total = n  # last "total" line wins; the grand total usually comes last

    if subtotal is None or not taxes or total is None:
        return None  # not a tax invoice, or too little was read to check anything

    computed = subtotal + sum(taxes.values())
    difference = total - computed
    if abs(difference) <= _tolerance(total):
        return None

    return ReconciliationFinding(
        check="tax_reconciliation",
        stated_total=total,
        computed_total=round(computed, 2),
        difference=difference,
        severity=_severity(difference, total),
        detail=(
            f"Taxable amount {subtotal:,.2f} plus "
            f"{', '.join(f'{k} {v:,.2f}' for k, v in taxes.items())} comes to {computed:,.2f}, "
            f"but the stated total is {total:,.2f} (difference {difference:,.2f})."
        ),
        suggested_action=(
            "Check the original document. This can be a genuine calculation error on the "
            "invoice, a misread digit during scanning, or a discount or rounding step this "
            "check did not see."
        ),
        components={"taxable_amount": subtotal, **taxes, "stated_total": total},
    )


# ---------------------------------------------------------------------------
# check 2: numbered line items should sum to the stated total
# ---------------------------------------------------------------------------
def check_itemised_total(text: str) -> ReconciliationFinding | None:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    total, total_idx = None, None
    for i, line in enumerate(lines):
        if TOTAL_RE.search(line) and not SUBTOTAL_RE.search(line):
            n = _last_number(line)
            if n is not None:
                total, total_idx = n, i  # keep the last total line found

    if total is None:
        return None

    items: list[float] = []
    for i, line in enumerate(lines):
        if total_idx is not None and i >= total_idx:
            continue
        if _has_skip_keyword(line) or TAX_RE.search(line) or SUBTOTAL_RE.search(line):
            continue
        if not re.search(r"[A-Za-z]{3,}", line):
            continue  # no real description on this line, probably not a charge
        n = _last_number(line)
        if n is None or n < 10:
            continue  # too small to be a labour or parts amount
        items.append(n)

    if len(items) < 2:
        return None  # not enough evidence to check anything

    computed = sum(items)
    difference = total - computed
    if abs(difference) <= _tolerance(total):
        return None

    return ReconciliationFinding(
        check="itemised_total",
        stated_total=total,
        computed_total=round(computed, 2),
        difference=difference,
        severity=_severity(difference, total),
        detail=(
            f"{len(items)} line items I can read sum to {computed:,.2f}, but the stated total "
            f"is {total:,.2f} (difference {difference:,.2f})."
        ),
        suggested_action=(
            "Check the original document before relying on this total. A line item may be "
            "illegible or misread, or the discrepancy may be genuine."
        ),
        components={"line_items": items, "stated_total": total},
    )


def check_document(text: str) -> list[ReconciliationFinding]:
    """Run every check. They are independent; either, both or neither may fire."""
    return [f for f in (check_tax_reconciliation(text), check_itemised_total(text)) if f]
