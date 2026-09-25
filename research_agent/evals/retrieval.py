"""Retrieval evaluation.

The six checks in agent/evaluator.py score the answer. They cannot score the
retrieval, and that gap is not academic: the most common RAG failure is that
the right passage was never retrieved at all. When that happens the answer is
fluent, every figure in it traces back to a tool result, and it is wrong. The
existing grounding check passes it happily, because the model grounded itself
in the wrong evidence rather than in no evidence.

So this measures the retriever directly, against a fixed set of questions
where we know which document should come back.

    bench --site erp.acme.com execute research_agent.evals.retrieval.run
    bench --site erp.acme.com execute research_agent.evals.retrieval.run --kwargs "{'with_ragas': 1}"

Two layers, deliberately separable:

  Layer 1, no dependencies. Did the expected file appear, and where in the
  ranking. Hit rate, MRR, recall at k. These need no API calls, run in
  seconds, and catch the failures that matter most: a chunking change that
  breaks clause boundaries, an embedding model swap, a permission regression.

  Layer 2, Ragas. Context precision and recall, judged by a model. Slower,
  costs money, and answers the subtler question of whether the retrieved
  passage actually supports the answer. Optional, because you should not need
  an API budget to know whether your retriever regressed.

Write the golden set against your own documents. The cases below are a shape
to copy, not a corpus.
"""

from __future__ import annotations

import json
import os
import time

import frappe

CASES_PATH = os.path.join(os.path.dirname(__file__), "retrieval_cases.json")


def load_cases(suite: str | None = None) -> list[dict]:
    if not os.path.exists(CASES_PATH):
        return []
    with open(CASES_PATH) as fh:
        cases = json.load(fh)
    return [c for c in cases if not suite or c.get("suite") == suite]


# ---------------------------------------------------------------------------
# layer 1: rank metrics, no API calls
# ---------------------------------------------------------------------------
def _rank_of(hits: list[dict], expected_file: str | None, expected_text: str | None) -> int:
    """1-based rank of the first correct hit, or 0 if it never appeared.

    A case can be pinned either to a file id or to a phrase that must appear
    in the passage. The phrase form is far more maintainable: file ids change
    when a document is re-uploaded, and the phrase is what you actually care
    about retrieving.
    """
    for i, h in enumerate(hits, start=1):
        if expected_file and h.get("file_id") == expected_file:
            return i
        if expected_text and expected_text.lower() in (h.get("text") or "").lower():
            return i
    return 0


def run_case(case: dict, limit: int = 8) -> dict:
    from research_agent.agent.rag import embed, retrieve

    started = time.time()
    try:
        vector = embed.embed_query(case["query"])
        result = retrieve.search(case["query"], vector, limit=limit)
    except Exception as e:
        return {"id": case["id"], "error": str(e), "hit": False, "rank": 0}

    hits = result["hits"]
    rank = _rank_of(hits, case.get("expect_file"), case.get("expect_text"))

    # A forbidden phrase appearing anywhere in the results is a hard failure,
    # not a scoring deduction. This is how a permission or deny-list
    # regression gets caught by the eval rather than by a customer.
    leaked = [
        p for p in case.get("forbid_text", [])
        if any(p.lower() in (h.get("text") or "").lower() for h in hits)
    ]

    return {
        "id": case["id"],
        "suite": case.get("suite"),
        "hit": bool(rank),
        "rank": rank,
        "reciprocal_rank": round(1 / rank, 3) if rank else 0.0,
        "returned": len(hits),
        "leaked": leaked,
        "hard_fail": bool(leaked),
        "elapsed_ms": int((time.time() - started) * 1000),
        "top_hit": (hits[0].get("file") if hits else None),
        "diagnostics": result["diagnostics"],
    }


def run(suite: str | None = None, user: str = "Administrator",
        limit: int = 8, with_ragas: int = 0) -> dict:
    """Run the golden set and print the numbers."""
    frappe.set_user(user)
    cases = load_cases(suite)
    if not cases:
        print("No cases in evals/retrieval_cases.json. Write some against your own documents.")
        return {"cases": 0}

    results = [run_case(c, limit=limit) for c in cases]
    for r in results:
        print(json.dumps(r, default=str))

    scored = [r for r in results if not r.get("error")]
    hits = [r for r in scored if r["hit"]]
    leaks = [r for r in scored if r["hard_fail"]]

    summary = {
        "suite": suite or "all",
        "user": user,
        "cases": len(results),
        "hit_rate": round(len(hits) / len(scored), 3) if scored else 0,
        "mrr": round(sum(r["reciprocal_rank"] for r in scored) / len(scored), 3) if scored else 0,
        "top1_rate": round(sum(1 for r in scored if r["rank"] == 1) / len(scored), 3) if scored else 0,
        "leaks": [r["id"] for r in leaks],
        "errors": [r["id"] for r in results if r.get("error")],
    }

    if with_ragas:
        summary["ragas"] = run_ragas(cases, limit=limit)

    print("\n" + json.dumps(summary, indent=2))
    if leaks:
        print("\nLEAKS DETECTED. Forbidden content was retrievable. Fix before shipping.")
    return summary


# ---------------------------------------------------------------------------
# layer 2: ragas
# ---------------------------------------------------------------------------
def run_ragas(cases: list[dict], limit: int = 8) -> dict:
    """Context precision and recall, judged by a model.

    Precision asks whether the passages we returned were relevant. Recall asks
    whether the passages needed to answer were present. They fail differently:
    low precision wastes context and invites the model to cite the wrong
    clause, low recall means the answer could not have been right.
    """
    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import context_precision, context_recall
    except ImportError:
        return {
            "skipped": "Ragas is not installed. Run: bench pip install ragas datasets",
        }

    from research_agent.agent.rag import embed, retrieve

    usable = [c for c in cases if c.get("ground_truth")]
    if not usable:
        return {"skipped": "No cases carry a ground_truth answer, which Ragas needs."}

    rows = {"question": [], "contexts": [], "ground_truth": [], "answer": []}
    for c in usable:
        try:
            vector = embed.embed_query(c["query"])
            hits = retrieve.search(c["query"], vector, limit=limit)["hits"]
        except Exception:
            continue
        rows["question"].append(c["query"])
        rows["contexts"].append([h["text"] for h in hits] or [""])
        rows["ground_truth"].append(c["ground_truth"])
        # Ragas wants an answer field. We are grading retrieval, not
        # generation, so the ground truth stands in and the two retrieval
        # metrics are unaffected by it.
        rows["answer"].append(c["ground_truth"])

    if not rows["question"]:
        return {"skipped": "No cases produced retrievable context."}

    key = frappe.get_cached_doc("Research Agent Settings").get_password(
        "openai_api_key", raise_exception=False
    )
    if key:
        os.environ.setdefault("OPENAI_API_KEY", key)

    scores = evaluate(
        Dataset.from_dict(rows), metrics=[context_precision, context_recall]
    )
    return {k: round(float(v), 3) for k, v in dict(scores).items()}


# ---------------------------------------------------------------------------
# permission regression, run as a restricted user
# ---------------------------------------------------------------------------
def run_permission_check(restricted_user: str, forbidden_phrases: list[str] | None = None) -> dict:
    """Prove a restricted user cannot retrieve what they should not see.

    Run this on every release, as a real user with real User Permissions. It
    is the one eval whose failure is a breach rather than a quality problem,
    and it cannot be run as Administrator because Administrator is exactly the
    user for whom the check is meaningless.
    """
    from research_agent.agent.rag import embed, retrieve

    if restricted_user in ("Administrator", "Guest"):
        frappe.throw("Run this as a genuinely restricted user, not Administrator.")

    phrases = forbidden_phrases or ["salary", "ctc", "gross pay", "increment"]
    frappe.set_user(restricted_user)

    findings = []
    for phrase in phrases:
        try:
            vector = embed.embed_query(phrase)
            hits = retrieve.search(phrase, vector, limit=10)["hits"]
        except Exception as e:
            findings.append({"phrase": phrase, "error": str(e)})
            continue
        findings.append(
            {
                "phrase": phrase,
                "returned": len(hits),
                "files": sorted({h["file"] for h in hits}),
                "attached_to": sorted({h.get("attached_to") for h in hits if h.get("attached_to")}),
            }
        )

    print(json.dumps({"user": restricted_user, "probes": findings}, indent=2))
    print(
        "\nRead every file above and confirm this user is genuinely allowed to open it in the "
        "desk. Anything they cannot open is a leak."
    )
    return {"user": restricted_user, "probes": findings}
