"""Retrieval permission tests.

The claim this app makes is that a user cannot retrieve a document they are
not allowed to read. That claim is worth proving on every push rather than
asserting in a README, so these tests stub frappe and drive the real
retriever code with three users who can see different things.

    python research_agent/tests/test_retrieval_permissions.py

The important cases are the negative ones. A retriever that returns good
results for a permitted user but also returns them for a restricted user is
not a partial success, it is a data breach with good recall.
"""

import base64
import importlib.util
import pathlib
import sys
import types

import numpy as np

# --------------------------------------------------------------- frappe stub
# A tiny fake site: three users, four files, four chunks. get_list("File")
# behaves the way ERPNext would, returning only what the current user may see.

FILES = {
    "F-invoice":  {"attached_to_doctype": "Purchase Invoice", "attached_to_name": "PI-001", "is_private": 0},
    "F-contract": {"attached_to_doctype": "Supplier",         "attached_to_name": "Acme",   "is_private": 0},
    "F-salary":   {"attached_to_doctype": "Salary Slip",      "attached_to_name": "SS-009", "is_private": 1},
    "F-loose":    {"attached_to_doctype": None,               "attached_to_name": None,     "is_private": 0},
}

CHUNKS = {
    "C1": {"source_file": "F-invoice",  "source_doctype": "Purchase Invoice", "source_docname": "PI-001",
           "chunk_text": "payment terms net 45 days", "file_name": "inv.pdf", "page_number": 1,
           "section_heading": "Terms", "chunk_index": 0},
    "C2": {"source_file": "F-contract", "source_doctype": "Supplier", "source_docname": "Acme",
           "chunk_text": "rate revision clause 7.2", "file_name": "msa.pdf", "page_number": 4,
           "section_heading": "Clause 7.2", "chunk_index": 0},
    "C3": {"source_file": "F-salary",   "source_doctype": "Salary Slip", "source_docname": "SS-009",
           "chunk_text": "gross salary 240000 per month", "file_name": "slip.pdf", "page_number": 1,
           "section_heading": None, "chunk_index": 0},
    "C4": {"source_file": "F-loose",    "source_doctype": None, "source_docname": None,
           "chunk_text": "unattached scratch document", "file_name": "notes.pdf", "page_number": 1,
           "section_heading": None, "chunk_index": 0},
}

# Which files each user can see through get_list, and which parent doctypes
# they hold read permission on. Deliberately not the same thing.
USERS = {
    "admin@test":    {"files": set(FILES), "doctypes": {"Purchase Invoice", "Supplier", "Salary Slip"}},
    "buyer@test":    {"files": {"F-invoice", "F-contract", "F-loose"}, "doctypes": {"Purchase Invoice", "Supplier"}},
    "restricted@test": {"files": {"F-invoice"}, "doctypes": {"Purchase Invoice"}},
    # Can see the File row but not the parent document. The gap the second
    # gate exists to close.
    "leaky@test":    {"files": {"F-invoice", "F-salary"}, "doctypes": {"Purchase Invoice"}},
}

STATE = {"user": "admin@test", "index_unattached": 0, "denied": ""}

frappe = types.ModuleType("frappe")
frappe.session = types.SimpleNamespace(user="admin@test")


def _get_list(doctype, filters=None, fields=None, pluck=None, limit_page_length=None, **kw):
    profile = USERS[STATE["user"]]
    if doctype == "File":
        f = filters or {}
        want_attached = f.get("attached_to_doctype") == ["is", "set"]
        out = []
        for fid, meta in FILES.items():
            if fid not in profile["files"]:
                continue
            has_parent = bool(meta["attached_to_doctype"])
            if want_attached and not has_parent:
                continue
            if not want_attached and has_parent:
                continue
            out.append(fid)
        return out if pluck else [{"name": x} for x in out]
    raise AssertionError(f"unexpected get_list on {doctype}")


def _get_all(doctype, filters=None, fields=None, limit_page_length=None, order_by=None, **kw):
    assert doctype == "Document Chunk"
    f = filters or {}
    names = list(CHUNKS)
    if "name" in f and f["name"][0] == "in":
        names = [n for n in names if n in f["name"][1]]
    if "embedding" in f:
        pass
    rows = []
    for n in sorted(names):
        row = {"name": n, "embedding": VECTORS[n], **CHUNKS[n]}
        rows.append(_fdict(row))
    return rows


def _has_permission(doctype, ptype="read", doc=None, **kw):
    return doctype in USERS[STATE["user"]]["doctypes"]


class _fdict(dict):
    """Mimics frappe._dict: attribute access as well as key access."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _sql(query, values=None, as_dict=False):
    if "count(name)" in query:
        return [_fdict({"n": len(CHUNKS), "m": "2026-01-01"})]
    if "match(chunk_text)" in query:
        q = (values or {}).get("q", "").lower()
        hits = [
            _fdict({"name": n, "score": 1.0})
            for n, c in CHUNKS.items()
            if any(w in c["chunk_text"].lower() for w in q.split())
        ]
        return hits
    return []


class _Settings(dict):
    def get(self, k, default=None):
        if k == "index_unattached_files":
            return STATE["index_unattached"]
        if k == "kb_denied_doctypes":
            return STATE["denied"]
        return default


frappe.get_list = _get_list
frappe.get_all = _get_all
frappe.has_permission = _has_permission
frappe.get_cached_doc = lambda dt: _Settings()
frappe.log_error = lambda **kw: None
frappe._ = lambda t: t
frappe.db = types.SimpleNamespace(sql=_sql)
sys.modules["frappe"] = frappe

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("research_agent", types.ModuleType("research_agent"))
sys.modules.setdefault("research_agent.agent", types.ModuleType("research_agent.agent"))
sys.modules.setdefault("research_agent.agent.rag", types.ModuleType("research_agent.agent.rag"))

_spec = importlib.util.spec_from_file_location(
    "retrieve", ROOT / "research_agent" / "agent" / "rag" / "retrieve.py"
)
retrieve = importlib.util.module_from_spec(_spec)
sys.modules["retrieve"] = retrieve
_spec.loader.exec_module(retrieve)

# Distinct unit vectors so each chunk is the nearest neighbour of its own probe
rng = np.random.default_rng(7)
RAW = {n: rng.normal(size=retrieve.EMBED_DIM) for n in CHUNKS}
VECTORS = {n: retrieve.pack(v) for n, v in RAW.items()}


def as_user(u):
    STATE["user"] = u
    frappe.session.user = u
    retrieve.invalidate_cache()


FAILURES = []


def check(name, condition):
    print(("PASS  " if condition else "FAIL  ") + name)
    if not condition:
        FAILURES.append(name)


def chunks_for(user, probe_chunk, query="payment terms clause salary"):
    as_user(user)
    return {h["chunk_id"] for h in retrieve.search(query, RAW[probe_chunk], limit=8)["hits"]}


# ------------------------------------------------------------------- tests
def test_packing_roundtrip():
    v = rng.normal(size=retrieve.EMBED_DIM)
    restored = retrieve.unpack(retrieve.pack(v)).astype(np.float32) / retrieve.QUANT_SCALE
    unit = v / np.linalg.norm(v)
    check("quantised vector keeps direction", float(unit @ (restored / np.linalg.norm(restored))) > 0.99)


def test_admin_sees_permitted():
    got = chunks_for("admin@test", "C1")
    check("admin retrieves the invoice chunk", "C1" in got)


def test_restricted_user_blocked():
    got = chunks_for("restricted@test", "C2")
    check("restricted user cannot reach the contract", "C2" not in got)
    check("restricted user still gets their own file", "C1" in chunks_for("restricted@test", "C1"))


def test_salary_never_retrievable():
    # Even for a user who can read both the File and the Salary Slip.
    got = chunks_for("admin@test", "C3", query="gross salary per month")
    check("salary chunk is denied even to an admin", "C3" not in got)


def test_parent_permission_gate():
    # leaky@test can see the File row but not the Salary Slip behind it.
    got = chunks_for("leaky@test", "C3", query="gross salary per month")
    check("file-readable but parent-unreadable is blocked", "C3" not in got)


def test_unattached_excluded_by_default():
    got = chunks_for("buyer@test", "C4", query="unattached scratch document")
    check("unattached file excluded by default", "C4" not in got)


def test_unattached_opt_in():
    STATE["index_unattached"] = 1
    try:
        got = chunks_for("buyer@test", "C4", query="unattached scratch document")
        check("unattached file returned once opted in", "C4" in got)
    finally:
        STATE["index_unattached"] = 0


def test_configured_denylist():
    STATE["denied"] = "Supplier"
    try:
        got = chunks_for("buyer@test", "C2")
        check("configured denylist blocks the contract", "C2" not in got)
    finally:
        STATE["denied"] = ""


def test_relevance_floor_drops_irrelevant():
    # The restricted user can only see the invoice chunk. Probing with an
    # unrelated vector must return nothing rather than the least-bad match.
    as_user("restricted@test")
    result = retrieve.search("gross salary per month", RAW["C3"], limit=8)
    check("irrelevant nearest neighbour is dropped by the floor", not result["hits"])
    check("empty result names which of the three causes it was",
          result["diagnostics"]["note"] is not None)


def test_empty_reason_distinguishes_causes():
    permission = retrieve._empty_reason(visible=0, corpus=100)
    unindexed = retrieve._empty_reason(visible=5, corpus=0)
    nomatch = retrieve._empty_reason(visible=5, corpus=100)
    check("permission boundary is named as such", "permission boundary" in permission)
    check("empty corpus is named as a setup gap", "setup gap" in unindexed)
    check("three causes give three different messages",
          len({permission, unindexed, nomatch}) == 3)


def test_keyword_half_respects_permission():
    as_user("restricted@test")
    kw = retrieve.keyword_search("rate revision clause")
    check("keyword search cannot reach the contract either",
          all(h["chunk"] != "C2" for h in kw))


def test_fusion_prefers_agreement():
    fused = retrieve.fuse(
        [{"chunk": "A", "score": 1, "retriever": "dense"}, {"chunk": "B", "score": 1, "retriever": "dense"}],
        [{"chunk": "B", "score": 1, "retriever": "keyword"}],
    )
    check("chunk found by both retrievers ranks first", fused[0]["chunk"] == "B")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    print("FAILURES:", FAILURES if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
