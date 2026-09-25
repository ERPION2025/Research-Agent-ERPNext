"""Embeddings.

Small file, but it is where the money goes and where the two easiest
mistakes live.

Mistake one is embedding the same text twice. Re-indexing a corpus after a
parser change re-embeds every chunk, and most chunks did not change. A hash
of the exact text embedded is enough to skip those, and it turns a full
reindex from a real bill into a rounding error.

Mistake two is embedding the query with a different model or dimension count
than the corpus. That does not error. It returns confident nonsense, because
two unrelated vector spaces still produce cosine numbers. So the model name
is stored on every chunk and checked at query time.

On dimensions: text-embedding-3-small is trained with Matryoshka
representation learning, which means the first 256 of its 1536 dimensions are
themselves a usable embedding. Asking the API for 256 directly cuts storage
and search cost to a sixth for a small accuracy loss. That trade is clearly
right at this corpus size, and it is what makes the whole index sit in worker
memory.
"""

from __future__ import annotations

import hashlib

import frappe
from frappe import _

from research_agent.agent.rag.retrieve import EMBED_DIM

# Per million tokens, USD. Only used for reporting spend back to the admin, so
# being a little stale is fine; being absent means nobody knows what indexing cost.
PRICE_PER_MTOK = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
}

# The API rejects oversized batches, and one huge request that fails wastes
# every embedding in it. 96 is comfortably inside the limit and small enough
# that a retry is cheap.
BATCH_SIZE = 96
MAX_CHARS_PER_INPUT = 30000


def _client():
    try:
        from openai import OpenAI
    except ImportError:
        frappe.throw(_("Python package 'openai' is not installed. Run: bench pip install openai"))

    settings = frappe.get_cached_doc("Research Agent Settings")
    key = settings.get_password("openai_api_key", raise_exception=False)
    if not key:
        frappe.throw(_("Document search needs an OpenAI API key. Add it in Research Agent Settings."))
    return OpenAI(api_key=key, timeout=120.0)


def model_name() -> str:
    return (
        frappe.get_cached_doc("Research Agent Settings").embedding_model
        or "text-embedding-3-small"
    )


def text_hash(text: str, model: str) -> str:
    """Fingerprint of exactly what was sent to the API.

    The model is part of the hash on purpose. Changing the model must
    invalidate every cached vector, because vectors from two models are not
    comparable and mixing them silently degrades every search.
    """
    return hashlib.sha256(f"{model}:{EMBED_DIM}:{text}".encode()).hexdigest()[:32]


def estimate_tokens(text: str) -> int:
    """Rough, deliberately. Exact counting needs tiktoken and a download; the
    only consumer is a cost display where 15 percent is close enough."""
    return max(1, len(text) // 4)


def embed_texts(texts: list[str], model: str | None = None) -> tuple[list[list[float]], dict]:
    """Embed a list of strings. Returns (vectors, usage).

    Order is preserved, because the caller pairs the result back onto chunk
    rows by index. Any reordering here would silently attach the wrong vector
    to the wrong chunk, which is the kind of bug that produces plausible
    garbage for months.
    """
    if not texts:
        return [], {"tokens": 0, "cost": 0.0, "calls": 0}

    model = model or model_name()
    client = _client()
    vectors: list[list[float]] = []
    tokens, calls = 0, 0

    for start in range(0, len(texts), BATCH_SIZE):
        batch = [t[:MAX_CHARS_PER_INPUT] if t else " " for t in texts[start : start + BATCH_SIZE]]
        resp = client.embeddings.create(model=model, input=batch, dimensions=EMBED_DIM)
        calls += 1
        tokens += getattr(resp.usage, "total_tokens", 0) or 0

        # The API documents that data comes back in input order, but it also
        # returns an index on each item. Sorting by it costs nothing and
        # removes the possibility entirely.
        for item in sorted(resp.data, key=lambda d: d.index):
            vectors.append(item.embedding)

    cost = tokens / 1_000_000 * PRICE_PER_MTOK.get(model, 0.02)
    return vectors, {"tokens": tokens, "cost": round(cost, 6), "calls": calls}


def embed_query(query: str) -> list[float]:
    """One query vector, using whatever model the corpus was built with.

    Query and corpus must share a vector space. If they do not, similarity
    numbers still come back and still look ordinary, so this checks rather
    than trusts.
    """
    model = model_name()
    indexed_with = frappe.db.sql(
        "select distinct embedding_model from `tabDocument Chunk` "
        "where ifnull(embedding_model, '') != '' limit 3",
        pluck=True,
    )
    if indexed_with and model not in indexed_with:
        frappe.throw(
            _(
                "The index was built with {0} but settings now say {1}. Searching across two "
                "embedding models returns meaningless similarity scores. Reindex, or set the "
                "model back."
            ).format(", ".join(indexed_with), model)
        )

    vectors, _usage = embed_texts([query], model=model)
    return vectors[0]


def embed_chunks(chunks: list[dict]) -> dict:
    """Embed chunk dicts in place, skipping any whose text is already indexed.

    Each chunk needs a 'chunk_text'. On return each has 'embedding' packed and
    'embedding_model' set, ready to insert as Document Chunk rows.
    """
    from research_agent.agent.rag.retrieve import pack

    model = model_name()
    to_embed, targets = [], []
    reused = 0

    for chunk in chunks:
        h = text_hash(chunk["chunk_text"], model)
        cached = frappe.db.get_value(
            "Document Chunk", {"content_hash": h}, ["embedding", "embedding_model"], as_dict=True
        )
        if cached and cached.embedding:
            chunk["embedding"] = cached.embedding
            chunk["embedding_model"] = cached.embedding_model
            chunk["content_hash"] = h
            reused += 1
            continue
        chunk["content_hash"] = h
        to_embed.append(chunk["chunk_text"])
        targets.append(chunk)

    usage = {"tokens": 0, "cost": 0.0, "calls": 0}
    if to_embed:
        vectors, usage = embed_texts(to_embed, model=model)
        for chunk, vector in zip(targets, vectors):
            chunk["embedding"] = pack(vector)
            chunk["embedding_model"] = model

    return {
        "embedded": len(to_embed),
        "reused_from_cache": reused,
        "tokens": usage["tokens"],
        "cost": usage["cost"],
        "api_calls": usage["calls"],
        "model": model,
    }
