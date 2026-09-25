"""Evaluator.

Reflexion needs a verdict before it can reflect. This module produces one
from four cheap checks plus one LLM judge. Cheap checks run first, and if
they all pass with room to spare the judge is skipped, which keeps the
common case to a single extra model call per trial.

Each check returns a score between 0 and 1 and a sentence of evidence. The
sentences are what get fed into the self-reflection prompt, not the scores,
because a number tells the model nothing about what to do differently.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import frappe


@dataclass
class Check:
    name: str
    score: float
    weight: float
    evidence: str


@dataclass
class Verdict:
    passed: bool
    score: float
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.score < 0.7]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "score": round(self.score, 3),
            "checks": [
                {"name": c.name, "score": round(c.score, 3), "weight": c.weight, "evidence": c.evidence}
                for c in self.checks
            ],
        }

    def critique_text(self) -> str:
        if not self.failures:
            return "All checks passed."
        return "\n".join(f"- {c.name} scored {c.score:.2f}: {c.evidence}" for c in self.failures)


NUMERIC = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def _grounding(draft: str, transcript: list[dict]) -> Check:
    """Do the numbers in the answer appear anywhere in the tool outputs?"""
    claimed = {n.replace(",", "").rstrip(".0") for n in NUMERIC.findall(draft or "")}
    claimed = {c for c in claimed if len(c) >= 3}
    if not claimed:
        return Check("grounding", 1.0, 0.30, "No specific figures asserted, nothing to ground.")

    blob = json.dumps([s.get("output") for s in transcript], default=str).replace(",", "")
    found = {c for c in claimed if c in blob}
    ratio = len(found) / len(claimed)
    missing = sorted(claimed - found)[:6]
    return Check(
        "grounding",
        ratio,
        0.30,
        (
            "Every figure traces back to a tool result."
            if ratio == 1
            else f"{len(claimed) - len(found)} of {len(claimed)} figures do not appear in any tool output: {missing}. "
            "Either these were computed in your head, or you rounded without saying so."
        ),
    )


def _erp_used(question: str, transcript: list[dict]) -> Check:
    """A question about our numbers must actually hit the ERP."""
    internal_words = (
        "our", "we ", "my ", "company", "revenue", "sales", "customer", "invoice",
        "stock", "inventory", "margin", "receivable", "payable", "order", "item",
        "branch", "warehouse", "employee", "purchase", "vendor", "supplier",
    )
    needs_erp = any(w in (question or "").lower() for w in internal_words)
    erp_calls = [s for s in transcript if str(s.get("tool", "")).startswith("erp_")]

    if not needs_erp:
        return Check("erp_grounded", 1.0, 0.25, "Question is about the outside world, ERP not required.")
    if not erp_calls:
        return Check(
            "erp_grounded",
            0.0,
            0.25,
            "The question is about company data but no ERPNext tool was called. "
            "The answer is guesswork. Query the ERP.",
        )
    return Check("erp_grounded", 1.0, 0.25, f"{len(erp_calls)} ERP queries backed the answer.")


def _artifacts(question: str, artifacts: list) -> Check:
    """Visual questions must produce a visual."""
    wants_visual = any(
        w in (question or "").lower()
        for w in ("chart", "graph", "plot", "dashboard", "trend", "compare", "breakdown", "split", "visual", "over time")
    )
    if not wants_visual:
        return Check("artifacts", 1.0, 0.15, "No visual explicitly required.")
    if not artifacts:
        return Check(
            "artifacts",
            0.0,
            0.15,
            "The user asked for something visual and no chart, table or metric was created. "
            "Call create_chart with the data you already have.",
        )
    return Check("artifacts", 1.0, 0.15, f"{len(artifacts)} artifacts produced.")


def _sources(transcript: list[dict]) -> Check:
    """Preferred-domain ratio across every web_search call in the trial."""
    ratios = []
    for step in transcript:
        if step.get("tool") != "web_search":
            continue
        out = step.get("output") or {}
        sq = (out.get("data") or {}).get("source_quality") if isinstance(out, dict) else None
        if sq and sq.get("total"):
            ratios.append(sq["ratio"])
    if not ratios:
        return Check("source_quality", 1.0, 0.10, "No web sources used.")
    avg = sum(ratios) / len(ratios)
    return Check(
        "source_quality",
        min(avg / 0.4, 1.0),
        0.10,
        f"{avg:.0%} of web results came from preferred domains."
        + ("" if avg >= 0.4 else " Add include_domains or a more specific query to pull better sources."),
    )


def _citation_integrity(draft: str, citations: list) -> Check:
    """Did document claims get cited, and does every marker resolve.

    Two failures, weighted differently. A fabricated marker scores zero
    outright: it manufactures the appearance of evidence, which is worse than
    an uncited claim because it defeats the audit the whole product rests on.
    Retrieving passages and then citing none is a softer failure, but it still
    means the reader cannot check the answer.
    """
    if not citations:
        return Check("citation_integrity", 1.0, 0.15, "No document passages were used.")

    import re

    cited = set(re.findall(r"\[(D\d{1,2})\]", draft or ""))
    known = {c["ref"] if isinstance(c, dict) else c.ref for c in citations}
    unresolved = cited - known

    if unresolved:
        return Check(
            "citation_integrity", 0.0, 0.15,
            f"The answer cites {sorted(unresolved)}, which match no retrieved passage. "
            "That is a fabricated citation. Cite only the markers you were given.",
        )
    if not cited:
        return Check(
            "citation_integrity", 0.0, 0.15,
            f"{len(known)} document passages were retrieved and none were cited. Every claim "
            "taken from a document needs its [D#] marker so the reader can open the source.",
        )

    coverage = len(cited) / len(known)
    return Check(
        "citation_integrity", 1.0, 0.15,
        f"{len(cited)} of {len(known)} retrieved passages cited, all resolving."
        + ("" if coverage > 0.3 else " Most retrieved passages went unused, which suggests the "
                                     "search was broader than the question needed."),
    )


JUDGE_PROMPT = """You are reviewing a draft answer written by a business analytics agent for a senior manager.

Question asked:
{question}

Draft answer:
{draft}

Tools the agent called, in order:
{tool_log}

Score the draft from 0 to 1 on each of these, being strict:
- completeness: does it answer every part of the question that was asked
- decision_usefulness: could the manager act on this, or is it a data dump
- honesty: does it flag gaps, assumptions and date ranges rather than papering over them

Reply with JSON only, no prose and no code fences:
{{"completeness": 0.0, "decision_usefulness": 0.0, "honesty": 0.0, "critique": "two sentences on the single most important thing to fix"}}"""


def _judge(question: str, draft: str, transcript: list[dict]) -> Check:
    from research_agent.agent.llm import get_llm

    tool_log = "\n".join(
        f"{i + 1}. {s.get('tool')}({json.dumps(s.get('arguments'), default=str)[:200]})"
        for i, s in enumerate(transcript)
    ) or "none"

    try:
        llm = get_llm(role="reflector", temperature=0.0)
        resp = llm.chat(
            messages=[
                {
                    "role": "user",
                    "content": JUDGE_PROMPT.format(question=question, draft=draft[:8000], tool_log=tool_log[:4000]),
                }
            ],
            max_tokens=600,
        )
        raw = (resp.content or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        obj = json.loads(match.group(0)) if match else {}
        scores = [float(obj.get(k, 0.5)) for k in ("completeness", "decision_usefulness", "honesty")]
        return Check("llm_judge", sum(scores) / 3, 0.20, obj.get("critique") or "No critique returned.")
    except Exception as e:
        frappe.log_error(title="Research Agent judge failed", message=frappe.get_traceback())
        return Check("llm_judge", 0.7, 0.20, f"Judge unavailable ({e}), scored neutral.")


def evaluate(
    question: str,
    draft: str,
    transcript: list[dict],
    artifacts: list,
    threshold: float = 0.75,
    use_judge: bool = True,
    citations: list | None = None,
) -> Verdict:
    checks = [
        _grounding(draft, transcript),
        _erp_used(question, transcript),
        _artifacts(question, artifacts),
        _sources(transcript),
        _citation_integrity(draft, citations or []),
    ]

    cheap_score = sum(c.score * c.weight for c in checks) / sum(c.weight for c in checks)
    if use_judge and cheap_score > 0.4:
        checks.append(_judge(question, draft, transcript))

    total_weight = sum(c.weight for c in checks)
    score = sum(c.score * c.weight for c in checks) / total_weight if total_weight else 0.0
    return Verdict(passed=score >= threshold, score=score, checks=checks)
