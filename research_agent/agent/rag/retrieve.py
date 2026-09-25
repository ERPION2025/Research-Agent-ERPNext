"""Retrieval, with permissions decided by ERPNext rather than by metadata.

This is the security-critical file in the RAG layer. Everything else is
plumbing; if this is wrong, the app leaks documents.

The design in one line: ERPNext decides what is visible, then we search only
that.

    1. frappe.get_list("File", ...)   ERPNext applies roles, User Permissions,
                                      share rules and permission query hooks
    2. mask the vector matrix to the surviving file ids
    3. brute-force cosine over the survivors

Most RAG systems invert this. They denormalise an ACL onto each vector,
filter inside the vector database, and hope the copy stays in step with the
source. That copy is the leak: a document gets reassigned to another company,
the reindex job fails quietly, and a stale row keeps serving the old
permission for a week.

Here there is no copy. Permission is read live from ERPNext on every query,
which means a permission revoked one second ago is honoured on the next
search. The cost is that we cannot use an approximate index, because ANN
structures do not support arbitrary post-hoc masking well.

At this corpus size that cost is not real. Under 10k files is roughly 40k to
60k chunks. At 256 dimensions in int8 the whole matrix is about 13MB, and a
masked dot product over it takes tens of milliseconds. Brute force also gives
exact recall, where an ANN index gives approximate recall and a tuning
parameter to get wrong.

If the corpus ever outgrows this (call it 500k chunks), the replacement is a
real vector database with the same two-phase shape: pre-filter on
denormalised ACL for speed, then re-verify the survivors against
frappe.has_permission before they enter a prompt. Do not drop phase two.
"""

from __future__ import annotations

import base64
import time

import frappe
from frappe import _

# Matryoshka truncation. text-embedding-3-small is trained so a 256-dim prefix
# is still a usable embedding, which cuts memory to a fifth for a small
# accuracy cost. Changing this invalidates every stored vector, so it is a
# constant rather than a setting.
EMBED_DIM = 256
QUANT_SCALE = 127.0

# Nearest-neighbour search always returns a neighbour. With no floor, a query
# about payroll against a corpus of purchase invoices still returns the least
# unrelated invoice, at a similarity of roughly zero, and the model has no way
# to tell that apart from a real hit. That is a hallucination vector, so
# results below the floor are dropped and the caller is told they were.
#
# 0.18 is empirical for text-embedding-3-small: unrelated text pairs sit near
# 0.0 to 0.10, loosely related text around 0.2 to 0.3. Raise it if the corpus
# is narrow and homogeneous.
MIN_SIMILARITY = 0.18

_MATRIX_CACHE: dict = {"version": None, "ids": None, "matrix": None, "loaded_at": 0}


# ---------------------------------------------------------------------------
# vector packing
# ---------------------------------------------------------------------------
def pack(vector) -> str:
    """Normalise, quantise to int8, base64. Stored on the Document Chunk row.

    Normalising at write time means cosine similarity is a plain dot product
    at read time, which is where the loop is hot.
    """
    import numpy as np

    v = np.asarray(vector, dtype=np.float32)[:EMBED_DIM]
    if v.shape[0] < EMBED_DIM:
        v = np.pad(v, (0, EMBED_DIM - v.shape[0]))
    norm = np.linalg.norm(v)
    if norm > 0:
        v = v / norm
    return base64.b64encode(np.round(v * QUANT_SCALE).astype(np.int8).tobytes()).decode()


def unpack(blob: str):
    import numpy as np

    return np.frombuffer(base64.b64decode(blob), dtype=np.int8)


# ---------------------------------------------------------------------------
# the matrix
# ---------------------------------------------------------------------------
def _index_version() -> str:
    """Cheap fingerprint of the chunk table. Changes when anything is indexed."""
    row = frappe.db.sql(
        "select count(name) as n, max(modified) as m from `tabDocument Chunk`", as_dict=True
    )
    r = row[0] if row else {}
    return f"{r.get('n') or 0}:{r.get('m')}"


def load_matrix():
    """Whole corpus as one int8 matrix, cached per worker process.

    Cached on the module rather than in Redis on purpose: this is 13MB of
    numpy that would have to be serialised and deserialised on every request
    if it lived in Redis, which costs more than the search it saves.
    """
    import numpy as np

    version = _index_version()
    if _MATRIX_CACHE["version"] == version and _MATRIX_CACHE["matrix"] is not None:
        return _MATRIX_CACHE["ids"], _MATRIX_CACHE["matrix"]

    rows = frappe.get_all(
        "Document Chunk",
        filters={"embedding": ["is", "set"]},
        fields=["name", "embedding"],
        limit_page_length=0,
        order_by="name asc",
    )
    if not rows:
        empty = np.zeros((0, EMBED_DIM), dtype=np.int8)
        _MATRIX_CACHE.update({"version": version, "ids": [], "matrix": empty, "loaded_at": time.time()})
        return [], empty

    ids, vectors = [], []
    for r in rows:
        try:
            v = unpack(r.embedding)
        except Exception:
            continue
        if v.shape[0] != EMBED_DIM:
            continue
        ids.append(r.name)
        vectors.append(v)

    matrix = np.vstack(vectors) if vectors else np.zeros((0, EMBED_DIM), dtype=np.int8)
    _MATRIX_CACHE.update({"version": version, "ids": ids, "matrix": matrix, "loaded_at": time.time()})
    return ids, matrix


def invalidate_cache():
    _MATRIX_CACHE.update({"version": None, "ids": None, "matrix": None})


# ---------------------------------------------------------------------------
# the permission mask
# ---------------------------------------------------------------------------
def visible_file_ids(extra_filters: dict | None = None) -> set[str]:
    """Files the *current user* may read, straight from ERPNext.

    frappe.get_list is doing the real work. It applies role permissions, User
    Permissions, share rules and any get_permission_query_conditions hook the
    site has installed. We never reimplement any of that, which is the point:
    a permission rule added to the site next year is honoured here without a
    line of code changing.

    Administrator is not special-cased. If Administrator cannot see a file
    through get_list, the agent should not see it either.
    """
    filters = {"attached_to_doctype": ["is", "set"]}
    filters.update(extra_filters or {})

    attached = set(
        frappe.get_list("File", filters=filters, pluck="name", limit_page_length=0)
    )

    # Files with no parent document carry no inherited permission, so they are
    # only visible when the site has explicitly opted them in. Silence is the
    # safe default here: an unattached file of unknown provenance should not
    # be searchable just because it exists.
    if frappe.get_cached_doc("Research Agent Settings").get("index_unattached_files"):
        loose = set(
            frappe.get_list(
                "File",
                filters={"attached_to_doctype": ["is", "not set"], "is_private": 0},
                pluck="name",
                limit_page_length=0,
            )
        )
        attached |= loose

    return attached


def _verify_chunk(chunk: dict) -> bool:
    """Second gate: re-check the parent document itself.

    The File row being readable is necessary but not sufficient. A File
    attached to a Salary Slip can be readable as a File while the Salary Slip
    is not readable by this user, and it is the Salary Slip's content that
    ended up in the chunk.

    So we check the parent. This is the check that stops the salary leak.
    """
    dt, dn = chunk.get("source_doctype"), chunk.get("source_docname")
    if not dt:
        return True
    if not frappe.has_permission(dt, ptype="read", doc=dn if dn else None):
        return False
    return True


def _denied_doctypes() -> set[str]:
    """Never searchable, whatever the permissions say.

    Permission checks answer "may this user read this document". They do not
    answer "should this content ever be retrievable as free text". Payroll is
    the clearest case: an HR user legitimately can read salary slips, but a
    chatbot surfacing a salary figure in an unrelated answer is a disclosure
    regardless of who asked. These are excluded at index time and again here,
    because a deny list enforced in only one place is not a deny list.
    """
    s = frappe.get_cached_doc("Research Agent Settings")
    configured = {d.strip() for d in (s.get("kb_denied_doctypes") or "").split(",") if d.strip()}
    return HARD_DENIED | configured


HARD_DENIED = {
    "Salary Slip",
    "Salary Structure",
    "Salary Structure Assignment",
    "Employee Tax Exemption Declaration",
    "Employee Tax Exemption Proof Submission",
    "Additional Salary",
    "Payroll Entry",
    "Employee Incentive",
    "Retention Bonus",
    "User",
    "Access Log",
}


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def dense_search(query_vector, limit: int = 40, candidate_pool: int = 400,
                 min_score: float | None = None) -> list[dict]:
    """Cosine over the permitted subset of the corpus, above a relevance floor."""
    import numpy as np

    ids, matrix = load_matrix()
    if not ids:
        return []

    permitted_files = visible_file_ids()
    if not permitted_files:
        return []

    # Map chunk ids to their file so the mask can be built without loading text
    meta = frappe.get_all(
        "Document Chunk",
        filters={"name": ["in", ids]},
        fields=["name", "source_file", "source_doctype", "source_docname"],
        limit_page_length=0,
    )
    denied = _denied_doctypes()
    by_id = {m.name: m for m in meta}

    mask = np.array(
        [
            (by_id.get(cid) is not None
             and by_id[cid].source_file in permitted_files
             and by_id[cid].source_doctype not in denied)
            for cid in ids
        ],
        dtype=bool,
    )
    if not mask.any():
        return []

    q = np.asarray(query_vector, dtype=np.float32)[:EMBED_DIM]
    if q.shape[0] < EMBED_DIM:
        q = np.pad(q, (0, EMBED_DIM - q.shape[0]))
    n = np.linalg.norm(q)
    if n > 0:
        q = q / n

    scores = (matrix[mask].astype(np.float32) / QUANT_SCALE) @ q
    kept_ids = [cid for cid, keep in zip(ids, mask) if keep]

    floor = MIN_SIMILARITY if min_score is None else min_score
    top = np.argsort(-scores)[: min(candidate_pool, scores.shape[0])]
    return [
        {"chunk": kept_ids[i], "score": float(scores[i]), "retriever": "dense"}
        for i in top[:limit]
        if float(scores[i]) >= floor
    ]


def keyword_search(query: str, limit: int = 20) -> list[dict]:
    """BM25 half of the hybrid, using MariaDB FULLTEXT.

    Dense retrieval is weak on exact identifiers: a part number, a clause
    reference, an invoice number. Those are precisely what people search
    contracts and scanned documents for, so the keyword half is not optional
    padding here.

    Permission is applied by intersecting with the same visible file set,
    never by trusting the SQL.
    """
    permitted = visible_file_ids()
    if not permitted or not (query or "").strip():
        return []

    try:
        rows = frappe.db.sql(
            """
            select name, match(chunk_text) against (%(q)s in natural language mode) as score
            from `tabDocument Chunk`
            where match(chunk_text) against (%(q)s in natural language mode)
            order by score desc limit %(lim)s
            """,
            {"q": query, "lim": limit * 4},
            as_dict=True,
        )
    except Exception:
        # No FULLTEXT index yet, or MariaDB refused the query. Dense-only is a
        # degraded result, not a failed one, so do not take the whole search down.
        frappe.log_error(title="Research Agent: keyword search unavailable",
                         message=frappe.get_traceback())
        return []

    if not rows:
        return []

    meta = {
        m.name: m
        for m in frappe.get_all(
            "Document Chunk",
            filters={"name": ["in", [r.name for r in rows]]},
            fields=["name", "source_file", "source_doctype"],
            limit_page_length=0,
        )
    }
    denied = _denied_doctypes()
    out = []
    for r in rows:
        m = meta.get(r.name)
        if not m or m.source_file not in permitted or m.source_doctype in denied:
            continue
        out.append({"chunk": r.name, "score": float(r.score), "retriever": "keyword"})
        if len(out) >= limit:
            break
    return out


def fuse(dense: list[dict], keyword: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal rank fusion.

    RRF rather than score blending because the two retrievers produce scores
    on incomparable scales (bounded cosine versus unbounded BM25), and any
    weighting between them would be a constant someone tuned once on one
    corpus and never revisited. RRF only uses rank, so it needs no tuning.
    """
    ranks: dict[str, float] = {}
    seen: dict[str, set] = {}
    for results in (dense, keyword):
        for rank, r in enumerate(results, start=1):
            ranks[r["chunk"]] = ranks.get(r["chunk"], 0.0) + 1.0 / (k + rank)
            seen.setdefault(r["chunk"], set()).add(r["retriever"])
    return [
        {"chunk": cid, "fused_score": score, "retrievers": sorted(seen[cid])}
        for cid, score in sorted(ranks.items(), key=lambda x: -x[1])
    ]


def hydrate(fused: list[dict], limit: int) -> list[dict]:
    """Load text for the survivors, re-verifying permission on each one.

    This is the post-verify gate. The mask above was built from a file id set;
    this checks the parent document itself, one by one, on the handful of
    chunks that are actually about to enter a prompt. Cheap at this size, and
    it is the difference between "probably fine" and "checked".
    """
    if not fused:
        return []

    ids = [f["chunk"] for f in fused[: limit * 3]]
    rows = {
        r.name: r
        for r in frappe.get_all(
            "Document Chunk",
            filters={"name": ["in", ids]},
            fields=[
                "name", "chunk_text", "source_file", "file_name", "source_doctype",
                "source_docname", "page_number", "section_heading", "chunk_index",
            ],
            limit_page_length=0,
        )
    }

    out = []
    for f in fused:
        row = rows.get(f["chunk"])
        if not row:
            continue
        if not _verify_chunk(row):
            continue
        out.append(
            {
                "chunk_id": row.name,
                "text": row.chunk_text,
                "file": row.file_name,
                "file_id": row.source_file,
                "attached_to": f"{row.source_doctype} {row.source_docname}".strip()
                if row.source_doctype else None,
                "page": row.page_number,
                "section": row.section_heading,
                "score": round(f["fused_score"], 5),
                "matched_by": f["retrievers"],
            }
        )
        if len(out) >= limit:
            break
    return out


def _empty_reason(visible: int, corpus: int) -> str:
    if corpus == 0:
        return ("Nothing is indexed yet. This is a setup gap, not an absence of "
                "documents. Say so rather than concluding the document does not exist.")
    if visible == 0:
        return ("This user cannot read any indexed document. This is a permission "
                "boundary, not an absence of documents. Say so plainly.")
    return ("Documents exist and are readable, but none were similar enough to the "
            "query to clear the relevance floor. Rephrase with the exact wording you "
            "expect to appear in the document, or say that nothing matched.")


def search(query: str, query_vector, limit: int = 8) -> dict:
    """Full pipeline. Returns hits plus enough diagnostics to debug a miss."""
    started = time.time()
    ids, matrix = load_matrix()

    dense = dense_search(query_vector, limit=40)
    keyword = keyword_search(query, limit=20)
    hits = hydrate(fuse(dense, keyword), limit)
    permitted = len(visible_file_ids())

    return {
        "query": query,
        "hits": hits,
        "diagnostics": {
            "corpus_chunks": len(ids),
            "dense_candidates": len(dense),
            "keyword_candidates": len(keyword),
            "returned": len(hits),
            "elapsed_ms": int((time.time() - started) * 1000),
            "visible_files": permitted,
            "similarity_floor": MIN_SIMILARITY,
            # An empty result has three quite different causes and the model
            # must not collapse them into "there is no such document". Naming
            # which one it was is the difference between an honest answer and
            # a confident wrong one.
            "note": _empty_reason(permitted, len(ids)) if not hits else None,
        },
    }
