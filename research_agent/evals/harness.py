"""Eval harness.

Run a fixed set of questions against the agent and print the evaluator scores.
This is how you find out whether a prompt change helped or just felt better.

    bench --site erp.local execute research_agent.evals.harness.run_suite
    bench --site erp.local execute research_agent.evals.harness.run_suite --kwargs "{'suite': 'finance'}"

Add cases in cases.json. Keep them boring and specific: a case whose right
answer changes every week is not a regression test.
"""

from __future__ import annotations

import json
import os
import time

import frappe

CASES_PATH = os.path.join(os.path.dirname(__file__), "cases.json")


def load_cases(suite: str | None = None) -> list[dict]:
    with open(CASES_PATH) as fh:
        cases = json.load(fh)
    return [c for c in cases if not suite or c.get("suite") == suite]


def run_suite(suite: str | None = None, user: str = "Administrator") -> dict:
    from research_agent.agent.loop import ReflexionAgent

    frappe.set_user(user)
    cases = load_cases(suite)
    results, started = [], time.time()

    for case in cases:
        session = frappe.get_doc(
            {
                "doctype": "Research Session",
                "title": f"[eval] {case['id']}",
                "prompt": case["prompt"],
                "status": "Queued",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()

        try:
            out = ReflexionAgent(session.name).run()
            doc = frappe.get_doc("Research Session", session.name)
            detail = json.loads(doc.eval_detail or "{}")
            row = {
                "id": case["id"],
                "score": out["score"],
                "trials": doc.trials_used,
                "tools": len([s for s in doc.steps if s.step_type == "Tool Call"]),
                "artifacts": len(doc.artifacts),
                "seconds": doc.duration_seconds,
                "checks": {c["name"]: c["score"] for c in detail.get("checks", [])},
                "expect_tools_met": all(
                    any(s.tool_name == t for s in doc.steps) for t in case.get("expect_tools", [])
                ),
                "forbidden_tools_called": [
                    t for t in case.get("forbid_tools", [])
                    if any(s.tool_name == t for s in doc.steps)
                ],
            }
        except Exception as e:
            row = {"id": case["id"], "score": 0.0, "error": str(e)}

        results.append(row)
        print(json.dumps(row, indent=2))

    for r in results:
        if r.get("forbidden_tools_called"):
            r["score"] = 0.0
            r["hard_fail"] = "called a forbidden tool"

    scored = [r["score"] for r in results if "score" in r]
    summary = {
        "suite": suite or "all",
        "cases": len(results),
        "mean_score": round(sum(scored) / len(scored), 3) if scored else 0,
        "passed": sum(1 for s in scored if s >= 0.75),
        "elapsed_seconds": round(time.time() - started, 1),
        "results": results,
    }
    print("\n" + json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    return summary
