"""Reconciliation tests.

Six fixtures, six real vendor invoices provided during development, not
synthetic text. Five reconcile cleanly. One does not: Paul Engineering
Works's slip sums its five line items to 10,580 but states a total of
10,680, a gap of 100 that survives two independent readings of the image.

That gap is what this module is for, and it is why the fixtures are real
rather than written to make the tests pass.

    python research_agent/tests/test_reconciliation.py
"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "reconcile", ROOT / "research_agent" / "agent" / "rag" / "reconcile.py"
)
reconcile = importlib.util.module_from_spec(_spec)
sys.modules["reconcile"] = reconcile
_spec.loader.exec_module(reconcile)

FAILURES = []


def check(name, condition):
    print(("PASS  " if condition else "FAIL  ") + name)
    if not condition:
        FAILURES.append(name)


# --------------------------------------------------------------- the six
RAMPRIT_1129 = """
CASH / CREDIT MEMO
Ramprit Spring Auto Garage
Bill No. 1129
Date: 5/7/18
Vehicle No.: HR55Y0963

Labour Charges For
1. Bellcrank open fitting two side - 3500
2. Rear patta pin bush four side open fitting - 1200
3. Front patta pin bush open fitting two side - 800

TOTAL 5500
"""

RAMPRIT_1130 = """
Bill No.: 1130
Date: 5/7/18
Lath work
Vehicle No.: HR5570963

1. Bellcrank Gola 7 Repair - 1750
2. Bush presu Ramming 12pc - 480
3. Presu Grounding 24 10Rs - 240
Lath work.

TOTAL 2470
"""

NEW_NITIN = """
TAX INVOICE
Invoice No.: NNAG231   Date: 20/09/2018
Vehicle No.: HR55Y5466

Sr No 1: Utility 2014 or 2016 - Tax Rate 18% - Qty 2 - Rate 1450 - Before Tax Amount 2900

CGST 9% 261
SGST 9% 261
Grand Total 3422
"""

PAUL_ENGINEERING = """
PAUL ENGINEERING WORKS
Vehicle No.: HR-55-X-1548

1. 1pc Kalas plate - 5800.00
2. Relig Bearing - 1230.00
3. Pailot Bearing - 350.00
4. Faiwill and pressure plate fashing - 1200.00
5. Mistri charge - 2000.00

Total - 10680.00
"""

LAXMI_GLASS = """
LAXMI GLASS HOUSE
Bill No. 22352  Date 04/07/18
M/s Rivigo Service Pvt Ltd

Qty 1: One front windshield broken - Rate 1350 - Amount 1350

TOTAL 1350
"""

ASHOK_MOTOR_GLASS = """
TAX INVOICE
Invoice Serial No.: 302   Invoice Date: 17/7/18
Vehicle No.: HR-55X9860

Ashok Leyland W/S RH Glass - HSN 7007 - GST% 18 - Qty 1PC - Rate 2017 - Amount 2017

Taxable Amount 2017
CGST 9% 181
SGST 9% 181
TOTAL AMOUNT 2379
"""


# ------------------------------------------------------------- the five that reconcile
def test_ramprit_1129_reconciles():
    check("Ramprit 1129 (3500+1200+800=5500) is silent", reconcile.check_document(RAMPRIT_1129) == [])


def test_ramprit_1130_reconciles():
    check("Ramprit 1130 (1750+480+240=2470) is silent", reconcile.check_document(RAMPRIT_1130) == [])


def test_new_nitin_tax_reconciles():
    check("New Nitin GST math (2900+261+261=3422) is silent",
          reconcile.check_document(NEW_NITIN) == [])


def test_ashok_motor_glass_tax_reconciles():
    check("Ashok Motor Glass GST math (2017+181+181=2379) is silent",
          reconcile.check_document(ASHOK_MOTOR_GLASS) == [])


def test_laxmi_single_item_is_silent():
    # Only one line item: not enough evidence to check anything. Silence here
    # is correct, not a missed case.
    check("a single-item bill is not flagged for lack of evidence",
          reconcile.check_document(LAXMI_GLASS) == [])


# --------------------------------------------------------- the one that does not
def test_paul_engineering_arithmetic_mismatch_detected():
    findings = reconcile.check_document(PAUL_ENGINEERING)
    check("the mismatch is caught", len(findings) == 1)
    f = findings[0]
    check("flagged via the itemised-total check", f.check == "itemised_total")
    check("five line items summed correctly to 10580", f.computed_total == 10580.0)
    check("stated total read as 10680", f.stated_total == 10680.0)
    check("difference computed as exactly 100", f.difference == 100.0)
    check("severity is proportionate, not alarmist", f.severity == "Medium")
    check("wording does not accuse the vendor of an error",
          "error on the original" not in f.detail.lower() and "wrong" not in f.detail.lower())
    check("wording allows for an OCR misread", "misread" in f.suggested_action.lower())


def test_paul_engineering_components_are_auditable():
    f = reconcile.check_tax_reconciliation(PAUL_ENGINEERING)
    check("no tax lines on this bill, so tax check stays silent", f is None)
    f = reconcile.check_itemised_total(PAUL_ENGINEERING)
    check("itemised components list all five amounts",
          f.components["line_items"] == [5800.0, 1230.0, 350.0, 1200.0, 2000.0])


# ------------------------------------------------------------------- guards
def test_empty_text():
    check("empty text produces no findings", reconcile.check_document("") == [])
    check("whitespace-only text produces no findings", reconcile.check_document("   \n  ") == [])


def test_tolerance_boundary():
    # Same shape as Paul Engineering, but the gap is inside the tolerance band.
    # A ~0.4% gap on a rounded handwritten total should not be flagged.
    text = """
    1. Frame work - 4980
    2. Paint job - 5000
    Total - 10020
    """
    check("a small gap within tolerance is not flagged", reconcile.check_document(text) == [])


def test_large_gap_is_high_severity():
    text = """
    1. Engine overhaul - 3000
    2. Gearbox service - 2000
    Total - 20000
    """
    findings = reconcile.check_document(text)
    check("a gap over 10 percent of the total is found", len(findings) == 1)
    check("and rated High rather than Medium", findings[0].severity == "High")


def test_rate_column_not_mistaken_for_a_charge():
    # A row that states both a unit rate and an amount must contribute only
    # the amount, never both, and rows carrying "Rate" are excluded outright
    # because they are the more common source of a false positive.
    text = """
    Item A - Rate 500 - Qty 2 - Amount 1000
    Item B - Rate 300 - Qty 1 - Amount 300
    Total 1300
    """
    check("rate/qty rows are excluded rather than double-counted",
          reconcile.check_document(text) == [])


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    print("FAILURES:", FAILURES if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
