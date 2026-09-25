"""Parsing.

Turns a File row into a list of pages of text. Nothing more.

Two decisions shape this file.

**pypdfium2, not PyMuPDF.** PyMuPDF is the better library and it is AGPL. An
AGPL dependency in an MIT app distributed through a marketplace is a licensing
problem for every user who installs it, not just for us. pypdfium2 is
Apache/BSD, ships prebuilt wheels with no system libraries, and installs on a
Frappe Cloud bench where `apt install` is not available. That last part is
decisive: nothing requiring a system package can ship here.

**Vision OCR, not Tesseract.** Tesseract needs a system binary, so on this
platform it is simply not an option. But it would lose anyway. Scanned
purchase invoices are dense tables, and Tesseract returns them as a soup of
words in reading order with the column structure destroyed. A vision model
returns markdown with the table intact, which is the difference between a
searchable document and noise.

The cost control is that OCR only runs on pages that need it. Most PDFs in an
ERP are born digital and carry a text layer; those cost nothing. Only genuine
scans hit the model, and the threshold below decides which is which.
"""

from __future__ import annotations

import base64
import io
import os

import frappe
from frappe import _

# A born-digital page reliably yields hundreds of characters. A scan yields a
# handful of stray marks the extractor mistook for glyphs. 120 sits well clear
# of both, and a nearly-blank digital page costs one OCR call to discover,
# which is the cheap direction to be wrong in.
TEXT_LAYER_THRESHOLD = 120

# 200 DPI is legible to a vision model without producing images so large that
# the request slows down or gets rejected.
RASTER_DPI = 200
MAX_PAGES = 200

OCR_PROMPT = """Transcribe this page exactly as it appears.

Rules:
- Return markdown. Preserve tables as markdown tables, with columns aligned to
  the original. Table structure carries the meaning on an invoice; losing it
  makes the page useless.
- Keep headings, numbering and clause references verbatim, including their
  numbers. "7.2" must stay "7.2".
- Transcribe figures exactly. Never round, never reformat, never convert a
  currency.
- If a region is illegible, write [illegible] rather than guessing. A guessed
  number is worse than a gap.
- No commentary, no preamble, no summary. The transcription only."""


# ---------------------------------------------------------------------------
# file access
# ---------------------------------------------------------------------------
def file_bytes(file_doc) -> bytes:
    """Read a File's content, private or public."""
    try:
        return file_doc.get_content()
    except Exception:
        path = file_doc.get_full_path()
        if not os.path.exists(path):
            frappe.throw(_("File {0} is missing from disk.").format(file_doc.name))
        with open(path, "rb") as fh:
            return fh.read()


def is_supported(file_name: str) -> tuple[bool, str]:
    ext = (file_name or "").lower().rsplit(".", 1)[-1] if "." in (file_name or "") else ""
    if ext == "pdf":
        return True, "pdf"
    if ext in ("png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "tif"):
        return True, "image"
    if ext in ("txt", "md", "csv"):
        return True, "text"
    return False, ext


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def _ocr_image(image_b64: str, media_type: str = "image/png") -> tuple[str, dict]:
    """One page image through a vision model."""
    from openai import OpenAI

    settings = frappe.get_cached_doc("Research Agent Settings")
    key = settings.get_password("openai_api_key", raise_exception=False)
    if not key:
        frappe.throw(_("OCR needs an OpenAI API key."))

    model = settings.ocr_model or "gpt-4.1-mini"
    resp = OpenAI(api_key=key, timeout=180.0).chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{media_type};base64,{image_b64}", "detail": "high"}},
                    {"type": "text", "text": OCR_PROMPT},
                ],
            }
        ],
    )
    usage = resp.usage
    # Vision pricing is per token like everything else; this is a reasonable
    # blended estimate for the mini tier, used only for the cost display.
    cost = (
        (getattr(usage, "prompt_tokens", 0) or 0) / 1_000_000 * 0.40
        + (getattr(usage, "completion_tokens", 0) or 0) / 1_000_000 * 1.60
    )
    return (resp.choices[0].message.content or "").strip(), {"cost": round(cost, 6), "pages": 1}


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def parse_pdf(data: bytes, allow_ocr: bool = True) -> tuple[list[dict], dict]:
    """Returns (pages, stats). Each page is {page, text, source}."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        frappe.throw(
            _("Python package 'pypdfium2' is not installed. Run: bench pip install pypdfium2")
        )

    pdf = pdfium.PdfDocument(io.BytesIO(data))
    pages, ocr_pages, cost = [], 0, 0.0
    total = min(len(pdf), MAX_PAGES)

    for i in range(total):
        page = pdf[i]
        try:
            text = (page.get_textpage().get_text_range() or "").strip()
        except Exception:
            text = ""

        if len(text) >= TEXT_LAYER_THRESHOLD:
            pages.append({"page": i + 1, "text": text, "source": "text_layer"})
            continue

        if not allow_ocr:
            # Recorded rather than dropped. A silently skipped page is a
            # retrieval gap nobody can see; a marked one shows up in the index
            # status and can be explained.
            pages.append({"page": i + 1, "text": text, "source": "no_text_layer_ocr_disabled"})
            continue

        try:
            bitmap = page.render(scale=RASTER_DPI / 72)
            buf = io.BytesIO()
            bitmap.to_pil().save(buf, format="PNG")
            ocr_text, usage = _ocr_image(base64.b64encode(buf.getvalue()).decode())
            cost += usage["cost"]
            ocr_pages += 1
            pages.append({"page": i + 1, "text": ocr_text, "source": "ocr"})
        except Exception as e:
            frappe.log_error(title=f"Research Agent OCR failed on page {i + 1}",
                             message=frappe.get_traceback())
            pages.append({"page": i + 1, "text": text, "source": f"ocr_failed: {e}"[:200]})

    return pages, {
        "page_count": total,
        "truncated": len(pdf) > MAX_PAGES,
        "pages_ocred": ocr_pages,
        "cost": round(cost, 6),
    }


def parse_image(data: bytes, media_type: str, allow_ocr: bool = True) -> tuple[list[dict], dict]:
    if not allow_ocr:
        return [], {"page_count": 1, "pages_ocred": 0, "cost": 0.0,
                    "skipped": "Image file and OCR is switched off."}
    text, usage = _ocr_image(base64.b64encode(data).decode(), media_type)
    return ([{"page": 1, "text": text, "source": "ocr"}] if text else [],
            {"page_count": 1, "pages_ocred": 1, "cost": usage["cost"]})


def parse_text(data: bytes) -> tuple[list[dict], dict]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    return ([{"page": 1, "text": text.strip(), "source": "plain"}] if text.strip() else [],
            {"page_count": 1, "pages_ocred": 0, "cost": 0.0})


def parse_file(file_doc) -> tuple[list[dict], dict]:
    """Dispatch on extension. Returns (pages, stats)."""
    supported, kind = is_supported(file_doc.file_name)
    if not supported:
        return [], {"skipped": f"Unsupported file type: .{kind}", "page_count": 0,
                    "pages_ocred": 0, "cost": 0.0}

    allow_ocr = bool(frappe.get_cached_doc("Research Agent Settings").ocr_scanned_pages)
    data = file_bytes(file_doc)

    if kind == "pdf":
        return parse_pdf(data, allow_ocr)
    if kind == "image":
        ext = file_doc.file_name.lower().rsplit(".", 1)[-1]
        media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "tif": "image/tiff"}.get(ext, f"image/{ext}")
        return parse_image(data, media, allow_ocr)
    return parse_text(data)
