"""Ingestion.

File arrives in ERPNext, ends up as searchable chunks. Everything here is
queued: parsing a 40-page scan takes a minute of wall clock and several
vision-model calls, and doing that inside the request that uploaded the file
would make attaching a PDF feel broken.

Three rules the pipeline enforces, each because the obvious version is wrong:

Nothing is indexed by default. A file is only indexed if the feature is on,
the file is attached to a document, that document's DocType is on the index
list, and it is not on the deny list. An app that quietly indexes everything
the moment it is installed is one that leaks the first payslip somebody
attaches to an Employee record.

Content is hashed before work begins. Re-running the indexer over a corpus
that has not changed should cost nothing, so a file whose bytes match the
last successful index is skipped before it is parsed, let alone embedded.

Failures are recorded per file, not swallowed. One corrupt PDF in ten
thousand should not stop the run, and it should not silently vanish either.
Document Index Status carries the status, the reason and the cost for every
file the pipeline has touched.
"""

from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import now

from research_agent.agent.rag import chunk as chunker
from research_agent.agent.rag import embed as embedder
from research_agent.agent.rag import parse as parser
from research_agent.agent.rag import reconcile
from research_agent.agent.rag.retrieve import HARD_DENIED, invalidate_cache

BATCH_ENQUEUE = 50


def _settings():
    return frappe.get_cached_doc("Research Agent Settings")


def enabled() -> bool:
    return bool(_settings().get("enable_knowledge_base"))


def _indexable_doctypes() -> set[str] | None:
    rows = _settings().get("index_doctypes") or []
    names = {r.document_type for r in rows if r.document_type}
    return names or None


def _denied() -> set[str]:
    configured = {
        d.strip() for d in (_settings().get("kb_denied_doctypes") or "").split(",") if d.strip()
    }
    return HARD_DENIED | configured


def should_index(file_doc) -> tuple[bool, str]:
    """Decide, and always say why. The reason is stored and shown to the admin,
    because 'why is my document not searchable' is otherwise unanswerable."""
    if not enabled():
        return False, "Document search is switched off."

    if not parser.is_supported(file_doc.file_name or file_doc.name)[0]:
        return False, f"Unsupported file type: {file_doc.file_name}"

    parent = file_doc.attached_to_doctype
    if not parent:
        if not _settings().get("index_unattached_files"):
            return False, (
                "Not attached to a document, so it inherits no permission. "
                "Turn on 'Index Unattached Files' if these are safe for every user."
            )
        return True, "Unattached file, indexing explicitly enabled."

    if parent in _denied():
        return False, f"'{parent}' is on the never-index list."

    allowed = _indexable_doctypes()
    if allowed and parent not in allowed:
        return False, f"'{parent}' is not on the index list."

    return True, ""


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _status_doc(file_doc):
    name = file_doc.name
    if frappe.db.exists("Document Index Status", name):
        return frappe.get_doc("Document Index Status", name)
    return frappe.get_doc(
        {
            "doctype": "Document Index Status",
            "source_file": name,
            "file_name": file_doc.file_name or name,
            "status": "Queued",
        }
    ).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------
def index_file(file_name: str, force: bool = False) -> dict:
    """Parse, chunk, embed and store one File. Safe to call repeatedly."""
    file_doc = frappe.get_doc("File", file_name)
    status = _status_doc(file_doc)

    ok, reason = should_index(file_doc)
    if not ok and not force:
        status.db_set({"status": "Skipped", "skip_reason": reason}, update_modified=False)
        frappe.db.commit()
        return {"file": file_name, "status": "Skipped", "reason": reason}

    try:
        status.db_set("status", "Parsing", update_modified=False)
        frappe.db.commit()

        data = parser.file_bytes(file_doc)
        digest = _content_hash(data)

        if not force and status.content_hash == digest and status.status == "Indexed":
            return {"file": file_name, "status": "Indexed", "reason": "Unchanged since last index"}

        pages, stats = parser.parse_file(file_doc)
        if not pages:
            status.db_set(
                {"status": "Skipped", "skip_reason": "No readable text found in this file.",
                 "content_hash": digest},
                update_modified=False,
            )
            frappe.db.commit()
            return {"file": file_name, "status": "Skipped", "reason": "No text"}

        chunks = chunker.chunk_pages(pages, file_doc)
        usage = embedder.embed_chunks(chunks)
        _replace_chunks(file_doc, chunks)

        # The document's own arithmetic, checked once per page. This is
        # independent of chunking and of the vector index: it runs on the
        # extracted text directly, because a clause boundary is irrelevant
        # to whether a total adds up.
        arithmetic = _check_arithmetic(pages, file_doc)

        status.db_set(
            {
                "status": "Indexed",
                "chunk_count": len(chunks),
                "page_count": stats.get("pages", len(pages)),
                "pages_ocred": stats.get("ocred", 0),
                "content_hash": digest,
                "indexed_on": now(),
                "index_cost": round(stats.get("ocr_cost", 0.0) + usage.get("cost", 0.0), 6),
                "arithmetic_flags": arithmetic,
                "error_log": "",
                "skip_reason": "",
            },
            update_modified=False,
        )
        frappe.db.commit()
        invalidate_cache()

        return {
            "file": file_name,
            "status": "Indexed",
            "chunks": len(chunks),
            "pages": stats.get("pages"),
            "ocred": stats.get("ocred", 0),
            "embedded": usage.get("embedded"),
            "reused": usage.get("reused_from_cache"),
            "cost": round(stats.get("ocr_cost", 0.0) + usage.get("cost", 0.0), 6),
            "arithmetic_flags": arithmetic or None,
        }

    except Exception as e:
        frappe.db.rollback()
        status = _status_doc(frappe.get_doc("File", file_name))
        status.db_set(
            {"status": "Failed", "error_log": frappe.get_traceback()[:100000]},
            update_modified=False,
        )
        frappe.db.commit()
        frappe.log_error(title=f"Research Agent: indexing failed for {file_name}",
                         message=frappe.get_traceback())
        return {"file": file_name, "status": "Failed", "error": str(e)}


def _check_arithmetic(pages: list[dict], file_doc) -> str:
    """Run the reconciliation checks over every page and return a summary.

    A document-level rather than a chunk-level check, because a total and the
    line items behind it can land in different chunks once split, while they
    are still on the same page as extracted. Checking pre-chunking is what
    keeps the clause boundary and the arithmetic boundary from interfering
    with each other.
    """
    notes = []
    for page in pages:
        findings = reconcile.check_document(page.get("text", ""))
        for f in findings:
            notes.append(f"p{page.get('page')}: {f.detail}")

    if not notes:
        return ""

    summary = " | ".join(notes)[:900]
    frappe.log_error(
        title=f"Research Agent: arithmetic mismatch in {file_doc.file_name}",
        message=summary,
    )
    return summary


def _replace_chunks(file_doc, chunks: list[dict]):
    """Delete then insert, rather than update in place.

    A reindex can produce a different number of chunks than last time, and
    reconciling old rows against new ones by position is how stale text
    survives a reindex and keeps being retrieved. Deleting first makes that
    impossible.
    """
    frappe.db.delete("Document Chunk", {"source_file": file_doc.name})

    for i, c in enumerate(chunks):
        frappe.get_doc(
            {
                "doctype": "Document Chunk",
                "source_file": file_doc.name,
                "file_name": file_doc.file_name or file_doc.name,
                "chunk_index": i,
                "source_doctype": file_doc.attached_to_doctype,
                "source_docname": file_doc.attached_to_name,
                "page_number": c.get("page"),
                "section_heading": (c.get("heading") or "")[:140],
                "chunk_text": c["chunk_text"],
                "token_estimate": embedder.estimate_tokens(c["chunk_text"]),
                "embedding": c.get("embedding"),
                "embedding_model": c.get("embedding_model"),
                "content_hash": c.get("content_hash"),
                "indexed_on": now(),
            }
        ).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# hooks and jobs
# ---------------------------------------------------------------------------
def on_file_insert(doc, method=None):
    """Queue a newly attached file. Never parses inline."""
    if not enabled():
        return
    ok, _reason = should_index(doc)
    if not ok:
        return
    frappe.enqueue(
        "research_agent.agent.rag.ingest.index_file",
        queue="long",
        timeout=1800,
        file_name=doc.name,
        enqueue_after_commit=True,
    )


def on_file_delete(doc, method=None):
    """Chunks must not outlive their file. A deleted document that is still
    retrievable is the worst possible failure of a permission model."""
    frappe.db.delete("Document Chunk", {"source_file": doc.name})
    if frappe.db.exists("Document Index Status", doc.name):
        frappe.delete_doc("Document Index Status", doc.name, force=True, ignore_permissions=True)
    invalidate_cache()


@frappe.whitelist()
def reindex_all(force: int = 0) -> dict:
    """Queue every eligible file. Called by the settings button."""
    frappe.only_for("System Manager")
    if not enabled():
        frappe.throw(_("Turn on Document Search first."))

    files = frappe.get_all(
        "File", filters={"attached_to_doctype": ["is", "set"]}, pluck="name", limit_page_length=0
    )
    if _settings().get("index_unattached_files"):
        files += frappe.get_all(
            "File",
            filters={"attached_to_doctype": ["is", "not set"], "is_private": 0},
            pluck="name",
            limit_page_length=0,
        )

    queued = 0
    for name in files:
        frappe.enqueue(
            "research_agent.agent.rag.ingest.index_file",
            queue="long",
            timeout=1800,
            file_name=name,
            force=bool(int(force)),
        )
        queued += 1

    return {
        "queued": queued,
        "note": "Files are indexed in the background. Watch Document Index Status for progress "
                "and per-file cost.",
    }


def retry_failed():
    """Daily. Transient failures (an API timeout, a rate limit) should not need
    a human to notice them."""
    if not enabled():
        return 0
    names = frappe.get_all(
        "Document Index Status", filters={"status": "Failed"}, pluck="name", limit_page_length=200
    )
    for n in names:
        frappe.enqueue(
            "research_agent.agent.rag.ingest.index_file", queue="long", timeout=1800, file_name=n
        )
    return len(names)


@frappe.whitelist()
def index_summary() -> dict:
    """What is indexed, what failed, what it cost. For the settings panel."""
    rows = frappe.get_all(
        "Document Index Status",
        fields=["status", "count(name) as count", "sum(chunk_count) as chunks",
                "sum(index_cost) as cost", "sum(pages_ocred) as ocred"],
        group_by="status",
    )
    flagged = frappe.db.count("Document Index Status", {"arithmetic_flags": ["is", "set"]})
    return {
        "by_status": rows,
        "total_chunks": frappe.db.count("Document Chunk"),
        "total_cost": round(sum(r.cost or 0 for r in rows), 4),
        "pages_ocred": sum(r.ocred or 0 for r in rows),
        "arithmetic_flagged": flagged,
    }
