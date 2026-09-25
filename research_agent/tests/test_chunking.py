"""Chunking tests.

Chunking decides what can be retrieved at all. A clause split down the middle
is unfindable no matter how good the embedding model is, so the boundaries
are worth testing directly rather than eyeballing once.

    python research_agent/tests/test_chunking.py

The contract case is the one that matters. Contracts are the document type
where a miss is expensive, and they are the reason the splitter is heading
aware instead of fixed size.
"""

import importlib.util
import pathlib
import sys
import types

frappe = types.ModuleType("frappe")
frappe.utils = types.ModuleType("frappe.utils")
frappe.utils.today = lambda: "2026-09-05"
frappe.utils.get_datetime = lambda v=None: v
frappe.get_cached_doc = lambda dt: types.SimpleNamespace(
    ocr_scanned_pages=1, ocr_model="gpt-4.1-mini", embedding_model="text-embedding-3-small"
)
frappe.db = types.SimpleNamespace(get_value=lambda *a, **k: None, exists=lambda *a, **k: False)
frappe.log_error = lambda **kw: None
frappe.throw = lambda msg, exc=None: (_ for _ in ()).throw(Exception(msg))
frappe._ = lambda t: t
sys.modules["frappe"] = frappe
sys.modules["frappe.utils"] = frappe.utils

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for m in ("research_agent", "research_agent.agent", "research_agent.agent.rag"):
    sys.modules.setdefault(m, types.ModuleType(m))

_spec = importlib.util.spec_from_file_location(
    "chunkmod", ROOT / "research_agent" / "agent" / "rag" / "chunk.py"
)
chunk = importlib.util.module_from_spec(_spec)
sys.modules["chunkmod"] = chunk
_spec.loader.exec_module(chunk)

FAILURES = []


def check(name, condition):
    print(("PASS  " if condition else "FAIL  ") + name)
    if not condition:
        FAILURES.append(name)


CONTRACT = """MASTER SUPPLY AGREEMENT

1. Definitions
In this agreement, "Goods" means the products listed in Schedule A, and
"Delivery Point" means the address nominated by the Buyer in writing.

7.2 Rate Revision
The unit rate may be revised once per financial year by written notice of not
less than sixty days. Any revision exceeding eight percent requires the prior
written consent of the Buyer. This clause survives termination.

7.3 Indemnity
The Supplier shall indemnify the Buyer against all claims arising from defects
in the Goods, save where such defects arise from the Buyer's own handling.

8. Termination
Either party may terminate on ninety days written notice.
"""

INVOICE = """TAX INVOICE

Vendor: Acme Components Pvt Ltd
Invoice No: ACM/2026/0412      Date: 12-08-2026

| Item      | Qty | Rate  | Amount |
|-----------|-----|-------|--------|
| Frame A2  | 500 | 2840  | 1420000|

Payment terms: net 45 days from date of invoice.
"""


def test_headings_detected():
    found = [chunk.detect_heading(ln) for ln in CONTRACT.splitlines()]
    hits = [h for h in found if h]
    check("numbered clause headings are detected", any("7.2" in h for h in hits))
    check("multiple headings found in one contract", len(hits) >= 3)


def test_clause_not_split_across_chunks():
    sections = chunk.split_page(CONTRACT)
    rate = [s for s in sections if "sixty days" in s["text"]]
    check("rate revision clause survives as one unit", len(rate) == 1)
    check("the whole clause body stays together",
          rate and "eight percent" in rate[0]["text"] and "survives termination" in rate[0]["text"])


def test_clause_carries_its_heading():
    sections = chunk.split_page(CONTRACT)
    rate = [s for s in sections if "sixty days" in s["text"]]
    check("clause is labelled with its own heading",
          rate and rate[0]["heading"] and "7.2" in rate[0]["heading"])


def test_indemnity_is_separate():
    sections = chunk.split_page(CONTRACT)
    both = [s for s in sections if "sixty days" in s["text"] and "indemnify" in s["text"]]
    check("distinct clauses are not merged together", not both)


def test_short_invoice_stays_whole():
    sections = chunk.split_page(INVOICE)
    check("a short invoice is not fragmented", len(sections) <= 2)
    joined = " ".join(s["text"] for s in sections)
    check("invoice table survives intact", "1420000" in joined and "net 45 days" in joined)


def test_long_text_is_split_with_overlap():
    body = "\n".join(f"Paragraph {i} with enough words in it to take up room." for i in range(200))
    sections = chunk._split_long(body, "Long section")
    check("oversized body is split", len(sections) > 1)
    check("every piece respects the ceiling",
          all(len(s["text"]) <= chunk.MAX_CHARS + chunk.OVERLAP_CHARS for s in sections))
    check("split pieces keep the heading", all(s["heading"] == "Long section" for s in sections))


def test_empty_input():
    check("empty page yields nothing", chunk.split_page("") == [])
    check("whitespace page yields nothing", chunk.split_page("   \n\n  ") == [])


def test_no_content_lost():
    sections = chunk.split_page(CONTRACT)
    joined = " ".join(s["text"] for s in sections)
    for phrase in ["Delivery Point", "eight percent", "indemnify the Buyer", "ninety days"]:
        check(f"content preserved: {phrase!r}", phrase in joined)


def test_context_line_is_prepended():
    file_doc = types.SimpleNamespace(
        file_name="acme-msa.pdf", attached_to_doctype="Supplier",
        attached_to_name="Acme Components", name="F-1",
    )
    line = chunk.context_line(file_doc, "7.2 Rate Revision", page=4, total_pages=12)
    check("context names the file", "acme-msa.pdf" in line)
    check("context names the parent document", "Acme" in line or "Supplier" in line)
    check("context names the clause", "7.2" in line)
    check("context is one short line", len(line) < 300 and "\n" not in line.strip())


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    print("FAILURES:", FAILURES if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
