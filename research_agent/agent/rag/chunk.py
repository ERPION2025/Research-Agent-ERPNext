"""Chunking.

This file decides whether contract search works. Everything else in the RAG
layer is mechanics; chunk boundaries are the thing that determines whether the
clause someone needs comes back or does not.

Three ideas, in order of how much they matter.

**Split on structure, not on length.** A fixed 800-token window cuts clause 7.2
in half and puts the exception in a different chunk from the rule it excepts.
Contracts and SOPs already carry their own boundaries as numbered headings, so
the splitter finds those and uses them. Length is a fallback for prose that has
no structure, not the primary rule.

**Give each chunk its context back.** A chunk reading "the rate may be revised
annually with 30 days notice" is nearly unretrievable: it names no party, no
contract, no date. Someone asking "can Acme raise prices mid-term" will never
match it. So a context line is prepended before embedding, naming the document,
the counterparty and the section. This is Anthropic's contextual retrieval, and
it is the single largest quality lever available here. It is done with string
formatting from ERPNext metadata rather than an LLM call per chunk, because the
metadata is already structured and correct, and 50k extra model calls to
restate it would be absurd.

**Do not chunk what should not be chunked.** A one-page scanned invoice is one
chunk. Splitting it separates the line items from the vendor name and makes
both halves worse.
"""

from __future__ import annotations

import re

# Sized for retrieval, not for the context window. Smaller chunks match more
# precisely; the model gets the surrounding document via kb_fetch_document when
# it needs more.
TARGET_CHARS = 1600
MAX_CHARS = 2600
MIN_CHARS = 120
OVERLAP_CHARS = 180

# Ordered most specific first. A line matching "7.2 Rate revision" should be
# read as a numbered clause, not as a generic short line.
HEADING_PATTERNS = [
    re.compile(r"^\s{0,4}(\d+(?:\.\d+){1,3})[.)]?\s+(\S.{0,110})$"),          # 7.2 Rate revision
    re.compile(r"^\s{0,4}(#{1,4})\s+(\S.{0,110})$"),                          # markdown
    re.compile(r"^\s{0,4}(ARTICLE|SECTION|CLAUSE|ANNEXURE|SCHEDULE|EXHIBIT|APPENDIX)\s+"
               r"([IVXLC\d]+[.)]?)\s*(.{0,90})$", re.IGNORECASE),
    re.compile(r"^\s{0,4}([A-Z][A-Z \-&/]{6,70})\s*$"),                       # ALL CAPS heading
]


def detect_heading(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 130:
        return None
    for pattern in HEADING_PATTERNS:
        match = pattern.match(stripped)
        if match:
            return " ".join(part for part in match.groups() if part and part != "#").strip()
    return None


def _split_long(text: str, heading: str | None) -> list[dict]:
    """Fall back to sentence-boundary packing when a section is oversized.

    Overlap exists so a fact sitting on a boundary appears in both halves.
    Without it, the one sentence that answers the question is exactly the one
    that gets cut down the middle.
    """
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text)
    out, buffer = [], ""

    for sentence in sentences:
        if len(buffer) + len(sentence) + 1 <= TARGET_CHARS or not buffer:
            buffer = f"{buffer} {sentence}".strip()
            continue
        out.append({"text": buffer, "heading": heading})
        tail = buffer[-OVERLAP_CHARS:]
        # Resume from a word boundary so the overlap is not half a word
        buffer = f"{tail[tail.find(' ') + 1:]} {sentence}".strip() if " " in tail else sentence

    if buffer.strip():
        out.append({"text": buffer.strip(), "heading": heading})
    return out


def split_page(text: str) -> list[dict]:
    """One page into sections, honouring headings where they exist."""
    if not (text or "").strip():
        return []

    lines = text.splitlines()
    sections: list[dict] = []
    heading, buffer = None, []

    def flush():
        body = "\n".join(buffer).strip()
        if not body:
            return
        if len(body) <= MAX_CHARS:
            sections.append({"text": body, "heading": heading})
        else:
            sections.extend(_split_long(body, heading))

    for line in lines:
        found = detect_heading(line)
        if found:
            flush()
            heading, buffer = found, [line]
        else:
            buffer.append(line)
    flush()

    # Merge stragglers. A heading on its own with two words under it is not a
    # retrievable unit; it belongs with the section that follows it.
    merged: list[dict] = []
    for section in sections:
        if merged and len(section["text"]) < MIN_CHARS:
            merged[-1]["text"] += "\n" + section["text"]
            continue
        if merged and len(merged[-1]["text"]) < MIN_CHARS:
            section["text"] = merged[-1]["text"] + "\n" + section["text"]
            section["heading"] = merged[-1]["heading"] or section["heading"]
            merged[-1] = section
            continue
        merged.append(section)
    return merged


def context_line(file_doc, heading: str | None, page: int, total_pages: int) -> str:
    """The contextual retrieval header, built from ERPNext metadata.

    Everything here is already known and correct on the File row and its
    parent, so it costs nothing and cannot hallucinate. Naming the parent
    document is what makes "the Acme contract" match a chunk that never says
    the word Acme.
    """
    bits = []
    if file_doc.attached_to_doctype:
        parent = f"{file_doc.attached_to_doctype} {file_doc.attached_to_name or ''}".strip()
        bits.append(f"From {parent}")
    bits.append(f"document '{file_doc.file_name}'")
    if heading:
        bits.append(f"section '{heading}'")
    if total_pages > 1:
        bits.append(f"page {page} of {total_pages}")
    return "[" + ", ".join(bits) + "]"


def chunk_pages(pages: list[dict], file_doc) -> list[dict]:
    """Pages into chunk dicts ready for embedding and insertion."""
    total_pages = len(pages)
    single_page_scan = total_pages == 1 and any(p.get("source") == "ocr" for p in pages)
    chunks: list[dict] = []

    for page in pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue

        # A one-page scan is one unit. Splitting a scanned invoice separates the
        # line items from the vendor name and degrades both halves.
        sections = (
            [{"text": text, "heading": None}]
            if single_page_scan and len(text) <= MAX_CHARS * 2
            else split_page(text)
        )

        for section in sections:
            body = section["text"].strip()
            if len(body) < 40:
                continue
            header = context_line(file_doc, section.get("heading"), page["page"], total_pages)
            chunks.append(
                {
                    "chunk_text": f"{header}\n{body}",
                    "raw_text": body,
                    "section_heading": (section.get("heading") or "")[:140],
                    "page_number": page["page"],
                    "chunk_index": len(chunks),
                    "token_estimate": max(1, len(body) // 4),
                    "source": page.get("source"),
                }
            )

    return chunks
