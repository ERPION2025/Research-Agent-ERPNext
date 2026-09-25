"""Knowledge base tools.

Tools 27 and 28. They give the agent the paperwork, which is the half of the
business it has never been able to see.

The tool descriptions below carry more weight than usual, because they are
where the single most important behaviour is enforced: the agent must not
compute figures from documents. The ERP already holds what was actually
spent, as structured data, permission-checked and arithmetically reliable. A
scanned invoice holds what the paperwork said. Those disagree more often than
anyone likes, and when they do, the ERP is the number and the document is the
evidence.

An agent that adds up OCR'd line items to answer "what did we spend" has
built a second, worse general ledger. The descriptions say so in the words
the model will read.
"""

from __future__ import annotations

import frappe
from frappe import _

from research_agent.agent.registry import tool


def _enabled():
    if not frappe.get_cached_doc("Research Agent Settings").get("enable_knowledge_base"):
        frappe.throw(
            _("Document search is switched off in Research Agent Settings."), frappe.PermissionError
        )


@tool(
    name="kb_search",
    category="Documents",
    description="""
    Search the text of PDFs, scans and images attached to ERPNext documents:
    contracts, signed agreements, vendor invoices, purchase orders, GRNs,
    specifications, policies.

    Use this for what the paperwork SAYS. For example: what payment terms a
    contract sets, which clause governs a rate revision, what a vendor's invoice
    actually stated, what a specification requires.

    Do NOT use this to compute business figures. What we spent, sold, produced or
    owe lives in the ERP as structured data and must come from the erp_ tools.
    Adding up numbers read out of a scanned document produces a second, worse set
    of accounts. If a document and the ERP disagree, that disagreement is itself
    the finding: report both and say which is which.

    Every result is permission-checked against the document the file is attached
    to, so results are limited to what this user could already open. An empty
    result does not mean the document does not exist; read the diagnostics, which
    say whether nothing is indexed, nothing was readable by this user, or nothing
    matched.

    Some hits carry an arithmetic_flag: the document's own line items or tax
    figures did not sum to its own stated total when it was indexed. That is the
    document disagreeing with itself, separate from any ERP comparison. If you
    cite a total from a flagged document, pass the flag on to the user rather
    than quoting the total as settled fact.

    Every passage comes back with a marker like D1. Cite it as [D1] inline,
    immediately after the claim it supports. Never write a file path, URL or page
    number as a citation yourself; the interface resolves markers into links to
    the real document. A marker you invent will not resolve and will be flagged.

    Quote the exact wording you find rather than paraphrasing a clause.
    """,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you are looking for. Include the exact terms you expect to "
                               "appear in the document, e.g. 'rate revision notice period', not "
                               "'pricing info'.",
            },
            "limit": {"type": "integer", "description": "Passages to return, 1 to 15. Default 8."},
            "attached_to_doctype": {
                "type": "string",
                "description": "Restrict to files attached to one DocType, e.g. 'Supplier' or "
                               "'Purchase Invoice'. Use when you know where the document lives.",
            },
            "attached_to_name": {
                "type": "string",
                "description": "Restrict to files on one specific document, e.g. a supplier name "
                               "or an invoice number. Use with attached_to_doctype.",
            },
        },
        "required": ["query"],
    },
)
def kb_search(query: str, limit: int = 8, attached_to_doctype: str | None = None,
              attached_to_name: str | None = None, context: dict | None = None):
    _enabled()
    from research_agent.agent.rag import citation, embed, retrieve

    limit = max(1, min(int(limit or 8), 15))
    vector = embed.embed_query(query)
    result = retrieve.search(query, vector, limit=limit)

    # Narrowing happens after retrieval rather than as a pre-filter. The scope
    # is a hint from the model about where it thinks the document lives, and
    # models get that wrong; filtering the corpus first would turn a wrong
    # guess into a confident "no such document".
    if attached_to_doctype:
        kept = [
            h for h in result["hits"]
            if (h.get("attached_to") or "").startswith(attached_to_doctype)
            and (not attached_to_name or attached_to_name in (h.get("attached_to") or ""))
        ]
        result["diagnostics"]["scope_filter"] = {
            "doctype": attached_to_doctype,
            "name": attached_to_name,
            "before": len(result["hits"]),
            "after": len(kept),
            "note": "Scope was applied after retrieval. If this removed everything, the document "
                    "may be attached elsewhere than you assumed; search again without the scope."
            if result["hits"] and not kept else None,
        }
        result["hits"] = kept

    # Markers are assigned by code, after filtering, so the model only ever
    # sees a marker for a passage that survived every permission check.
    _attach_arithmetic_flags(result["hits"])
    result["hits"] = citation.register(context or {}, result["hits"])
    result["citing"] = (
        "Cite these with their [D#] marker inline. Do not write file paths or URLs yourself."
    )
    return result


def _attach_arithmetic_flags(hits: list[dict]):
    """Carry the ingest-time self-consistency finding onto each hit.

    Checked once per file at index time, not per search, so this is a cheap
    lookup rather than a recomputation. A hit citing a total from a flagged
    document should not be presented as settled fact.
    """
    file_ids = {h.get("file_id") for h in hits if h.get("file_id")}
    if not file_ids:
        return
    flags = {
        r.source_file: r.arithmetic_flags
        for r in frappe.get_all(
            "Document Index Status",
            filters={"source_file": ["in", list(file_ids)], "arithmetic_flags": ["is", "set"]},
            fields=["source_file", "arithmetic_flags"],
        )
        if r.arithmetic_flags
    }
    for h in hits:
        flag = flags.get(h.get("file_id"))
        if flag:
            h["arithmetic_flag"] = flag


@tool(
    name="kb_read_document",
    category="Documents",
    description="""
    Read a whole indexed document in order, rather than the scattered passages
    kb_search returns.

    Use this when kb_search found the right document but you need the surrounding
    context to answer properly: reading a full clause with its sub-clauses,
    checking whether a term is qualified elsewhere, or confirming that a passage
    means what it appears to mean in isolation.

    Pass the file_id from a kb_search result. Long documents are truncated, and
    the response says so.
    """,
    parameters={
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "The file_id from a kb_search hit"},
            "from_page": {"type": "integer"},
            "to_page": {"type": "integer"},
        },
        "required": ["file_id"],
    },
)
def kb_read_document(file_id: str, from_page: int | None = None, to_page: int | None = None):
    _enabled()
    from research_agent.agent.rag.retrieve import _verify_chunk, visible_file_ids

    if file_id not in visible_file_ids():
        frappe.throw(
            _("You do not have access to that document, or it is not indexed."),
            frappe.PermissionError,
        )

    filters = {"source_file": file_id}
    if from_page:
        filters["page_number"] = [">=", int(from_page)]
    if to_page:
        filters["page_number"] = (
            ["between", [int(from_page or 1), int(to_page)]] if from_page else ["<=", int(to_page)]
        )

    rows = frappe.get_all(
        "Document Chunk",
        filters=filters,
        fields=["name", "chunk_text", "page_number", "section_heading", "chunk_index",
                "file_name", "source_doctype", "source_docname"],
        order_by="chunk_index asc",
        limit_page_length=0,
    )
    if not rows:
        return {"file_id": file_id, "error": "No indexed content for that file."}

    # Same second gate as retrieval. A tool that reads a whole document must
    # not be a way around the check the search path applies.
    rows = [r for r in rows if _verify_chunk(r)]
    if not rows:
        frappe.throw(_("You cannot read the document this file is attached to."),
                     frappe.PermissionError)

    budget, out, truncated = 40000, [], False
    used = 0
    for r in rows:
        if used + len(r.chunk_text) > budget:
            truncated = True
            break
        out.append({"page": r.page_number, "section": r.section_heading, "text": r.chunk_text})
        used += len(r.chunk_text)

    return {
        "file_id": file_id,
        "file": rows[0].file_name,
        "attached_to": f"{rows[0].source_doctype} {rows[0].source_docname}".strip()
        if rows[0].source_doctype else None,
        "passages": out,
        "truncated": truncated,
        "note": "Truncated. Narrow with from_page and to_page to read the rest."
        if truncated else None,
    }


@tool(
    name="kb_index_status",
    category="Documents",
    description="""
    Check what is actually indexed before concluding a document does not exist.
    Returns counts by status and how many files failed to index.

    Call this when a search comes back empty and you are about to tell the user
    nothing was found. 'Nothing is indexed' and 'nothing matched' are different
    answers and the user needs the right one.
    """,
    parameters={"type": "object", "properties": {}},
)
def kb_index_status():
    _enabled()
    from research_agent.agent.rag.ingest import index_summary

    summary = index_summary()
    summary["visible_to_you"] = len(
        frappe.get_list("File", filters={"attached_to_doctype": ["is", "set"]},
                        pluck="name", limit_page_length=0)
    )
    return summary
