"""Citations.

The rule this module exists to enforce: the model writes a marker, code owns
the link.

A model asked to cite its sources will happily write
`[contract.pdf, page 4](/files/contract.pdf)` and be wrong about the page, the
filename, or both. It is not lying; it is producing text that looks like the
text it has seen, and a plausible file path is easy to produce. For a product
whose entire claim is that answers are auditable, a citation that does not
resolve is worse than no citation, because it converts "I do not know" into
"I have evidence" without any evidence.

So the split is:

    kb_search        assigns D1, D2, D3 to the passages it actually returned
                     and registers the real file, page and URL against them
    the model        writes [D2] in its prose, and nothing else
    this module      turns [D2] into a link, using the registered mapping

The model cannot invent a working link because it never writes one. And when
it writes [D7] with only three passages retrieved, that is detectable, which
is what `validate` does. An unresolved marker is a fabricated citation and it
is surfaced on the session rather than quietly dropped.
"""

from __future__ import annotations

import re

import frappe

# D1, D2, ... Deliberately not a natural word or a bracketed number, because
# both collide with ordinary prose and with markdown footnote syntax.
MARKER_RE = re.compile(r"\[(D\d{1,2})\]")


def register(context: dict, hits: list[dict]) -> list[dict]:
    """Give each retrieved passage a marker and record where it came from.

    Markers are stable within a session: the same passage retrieved twice by
    two different searches keeps its first marker, so the answer does not end
    up citing D2 and D9 for the same clause.
    """
    session = (context or {}).get("session")
    if not session or not hits:
        return hits

    session.reload()
    existing = {c.source_file + "#" + str(c.page_number or 0): c.ref for c in session.citations}
    next_index = len(session.citations) + 1
    added = False

    for hit in hits:
        key = f"{hit.get('file_id')}#{hit.get('page') or 0}"
        if key in existing:
            hit["ref"] = existing[key]
            continue

        ref = f"D{next_index}"
        next_index += 1
        existing[key] = ref
        hit["ref"] = ref
        added = True

        session.append(
            "citations",
            {
                "ref": ref,
                "file_name": hit.get("file"),
                "page_number": hit.get("page"),
                "section_heading": (hit.get("section") or "")[:140],
                "source_file": hit.get("file_id"),
                "file_url": _file_url(hit.get("file_id")),
                "source_doctype": (hit.get("attached_to") or "").split(" ")[0] or None,
                "source_docname": " ".join((hit.get("attached_to") or "").split(" ")[1:]) or None,
                "snippet": (hit.get("text") or "")[:1000],
                "used": 0,
            },
        )

    if added:
        session.save(ignore_permissions=True)
        frappe.db.commit()
    return hits


def _file_url(file_id: str | None) -> str:
    """Resolved once, at registration, from the File row itself.

    Not constructed from the filename later. A path built by string
    concatenation is a path that can point at a file the user may not open.
    """
    if not file_id:
        return ""
    return frappe.db.get_value("File", file_id, "file_url") or f"/app/file/{file_id}"


def validate(context: dict, draft: str) -> dict:
    """Check every marker in the answer resolves, and mark the ones used.

    Returns the marker audit. Unresolved markers are the interesting output:
    they mean the model referenced a passage that was never retrieved.
    """
    session = (context or {}).get("session")
    if not session:
        return {"used": [], "unresolved": [], "unused": []}

    session.reload()
    known = {c.ref: c for c in session.citations}
    cited = set(MARKER_RE.findall(draft or ""))

    unresolved = sorted(cited - set(known))
    used = sorted(cited & set(known))
    unused = sorted(set(known) - cited)

    for ref, row in known.items():
        row.used = 1 if ref in cited else 0

    session.uncited_markers = ", ".join(unresolved)
    session.save(ignore_permissions=True)
    frappe.db.commit()

    return {"used": used, "unresolved": unresolved, "unused": unused}


def prompt_block(context: dict) -> str:
    """What the model is told about the markers available to it."""
    session = (context or {}).get("session")
    if not session:
        return ""
    session.reload()
    if not session.citations:
        return ""

    lines = [
        "",
        "Passages retrieved from documents, with the marker to cite each one:",
    ]
    for c in session.citations:
        where = f"{c.file_name}"
        if c.page_number:
            where += f", page {c.page_number}"
        if c.section_heading:
            where += f", {c.section_heading}"
        lines.append(f"  [{c.ref}] {where}")
    lines.append("")
    lines.append(
        "Write [D1] style markers inline, immediately after the claim they support. Write nothing "
        "else as a citation: no file paths, no URLs, no page numbers of your own. The interface "
        "turns each marker into a link to the actual document. A marker not in the list above "
        "will not resolve and will be flagged as fabricated."
    )
    return "\n".join(lines)


def resolve_for_display(session) -> list[dict]:
    """Citation list for the UI, in marker order."""
    out = []
    for c in sorted(session.citations, key=lambda c: int(c.ref[1:]) if c.ref[1:].isdigit() else 0):
        out.append(
            {
                "ref": c.ref,
                "file": c.file_name,
                "page": c.page_number,
                "section": c.section_heading,
                "url": c.file_url,
                "attached_to": f"{c.source_doctype} {c.source_docname}".strip()
                if c.source_doctype else None,
                "snippet": c.snippet,
                "used": bool(c.used),
            }
        )
    return out
