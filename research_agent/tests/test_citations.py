"""Citation integrity tests.

The claim is that a citation in the answer always points at a passage that
was really retrieved. That is only worth claiming if the fabricated case is
detected, so these tests spend most of their effort on the failure paths.
"""

import importlib.util
import pathlib
import re
import sys
import types

frappe = types.ModuleType("frappe")
frappe.db = types.SimpleNamespace(get_value=lambda *a, **k: "/private/files/msa.pdf", commit=lambda: None)
frappe.log_error = lambda **kw: None
frappe._ = lambda t: t
sys.modules["frappe"] = frappe

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for m in ("research_agent", "research_agent.agent", "research_agent.agent.rag"):
    sys.modules.setdefault(m, types.ModuleType(m))

_spec = importlib.util.spec_from_file_location(
    "cit", ROOT / "research_agent" / "agent" / "rag" / "citation.py"
)
cit = importlib.util.module_from_spec(_spec)
sys.modules["cit"] = cit
_spec.loader.exec_module(cit)

FAILURES = []


def check(name, condition):
    print(("PASS  " if condition else "FAIL  ") + name)
    if not condition:
        FAILURES.append(name)


class FakeRow(dict):
    def __getattr__(self, k):
        return self.get(k)

    def __setattr__(self, k, v):
        self[k] = v


class FakeSession:
    def __init__(self):
        self.citations = []
        self.uncited_markers = ""
        self.saved = 0

    def reload(self):
        pass

    def append(self, field, row):
        self.citations.append(FakeRow(row))

    def save(self, **kw):
        self.saved += 1


HITS = [
    {"file_id": "F-1", "file": "acme-msa.pdf", "page": 4, "section": "7.2 Rate Revision",
     "text": "revised by written notice of not less than sixty days", "attached_to": "Supplier Acme"},
    {"file_id": "F-2", "file": "inv-0412.pdf", "page": 1, "section": None,
     "text": "payment terms net 45 days", "attached_to": "Purchase Invoice PI-9"},
]


def test_markers_assigned_in_order():
    s = FakeSession()
    hits = cit.register({"session": s}, [dict(h) for h in HITS])
    check("first passage gets D1", hits[0]["ref"] == "D1")
    check("second passage gets D2", hits[1]["ref"] == "D2")
    check("both recorded on the session", len(s.citations) == 2)


def test_same_passage_keeps_its_marker():
    s = FakeSession()
    cit.register({"session": s}, [dict(HITS[0])])
    again = cit.register({"session": s}, [dict(HITS[0])])
    check("a repeated passage reuses its marker", again[0]["ref"] == "D1")
    check("no duplicate citation row created", len(s.citations) == 1)


def test_url_comes_from_the_file_row():
    s = FakeSession()
    cit.register({"session": s}, [dict(HITS[0])])
    check("url resolved from the File record, not built from the name",
          s.citations[0].file_url == "/private/files/msa.pdf")


def test_valid_markers_marked_used():
    s = FakeSession()
    cit.register({"session": s}, [dict(h) for h in HITS])
    audit = cit.validate({"session": s}, "Rates need sixty days notice [D1].")
    check("cited marker recorded as used", audit["used"] == ["D1"])
    check("uncited marker reported as unused", audit["unused"] == ["D2"])
    check("no unresolved markers", audit["unresolved"] == [])
    check("used flag set on the row", s.citations[0].used == 1)
    check("unused flag cleared on the row", s.citations[1].used == 0)


def test_fabricated_marker_detected():
    s = FakeSession()
    cit.register({"session": s}, [dict(h) for h in HITS])
    audit = cit.validate({"session": s}, "The policy says thirty days [D7].")
    check("invented marker is flagged", audit["unresolved"] == ["D7"])
    check("flag is persisted on the session", s.uncited_markers == "D7")


def test_prompt_block_lists_markers():
    s = FakeSession()
    cit.register({"session": s}, [dict(h) for h in HITS])
    block = cit.prompt_block({"session": s})
    check("prompt lists D1 with its location", "[D1]" in block and "page 4" in block)
    check("prompt forbids the model writing URLs", "no URLs" in block or "no file paths" in block)


def test_no_session_is_safe():
    check("register without a session returns hits unchanged",
          cit.register({}, [dict(HITS[0])])[0].get("ref") is None)
    check("validate without a session returns empty audit",
          cit.validate({}, "text [D1]")["unresolved"] == [])
    check("prompt block without a session is empty", cit.prompt_block({}) == "")


def test_marker_regex_is_narrow():
    # Ordinary prose and markdown links must not be mistaken for markers.
    text = "See [Documentation](http://x) and item [12] and [Draft] and [D3]."
    check("only D-markers match", re.findall(cit.MARKER_RE, text) == ["D3"])


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    print("FAILURES:", FAILURES if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
