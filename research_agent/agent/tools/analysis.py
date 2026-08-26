"""Code-as-plan analysis tool.

The model writes a short Python snippet instead of chaining ten narrow tools.
It runs inside frappe.utils.safe_exec, the same sandbox Server Scripts use:
no imports, no file system, no network, no frappe.db writes. pandas and a few
maths helpers are injected explicitly.

The snippet reads whatever the model passes in as `data` and must set
`result`. Anything printed is captured and returned as logs, which is what
makes the reflexion step able to see why an attempt went wrong.
"""

from __future__ import annotations

import json

import frappe

from research_agent.agent.registry import tool

MAX_OUTPUT_CHARS = 20000


def _namespace(data):
    ns = {"data": data, "result": None}
    try:
        import pandas as pd

        ns["pd"] = pd
        if isinstance(data, list) and data and isinstance(data[0], dict):
            ns["df"] = pd.DataFrame(data)
        elif isinstance(data, dict) and isinstance(data.get("rows"), list):
            ns["df"] = pd.DataFrame(data["rows"])
    except ImportError:
        pass

    import datetime
    import math
    import statistics

    ns.update(
        {
            "math": math,
            "statistics": statistics,
            "datetime": datetime,
            "flt": frappe.utils.flt,
            "cint": frappe.utils.cint,
            "getdate": frappe.utils.getdate,
            "add_months": frappe.utils.add_months,
            "fmt_money": frappe.utils.fmt_money,
        }
    )
    return ns


@tool(
    name="analyse_data",
    category="Analysis",
    description="""
    Run a short Python snippet over data you already fetched, for maths the other
    tools cannot do: growth rates, cohorts, ageing buckets, moving averages,
    concentration ratios, contribution margin, correlations.

    The snippet runs in a sandbox. No imports, no network, no file access, no writes
    to the ERP. Available names: data (whatever you passed), df (a pandas DataFrame
    when data is a list of dicts or has a 'rows' key), pd, math, statistics,
    datetime, flt, cint, getdate, add_months, fmt_money.

    You must assign the answer to a variable named `result`. Use print() for
    intermediate checks; the printed output comes back to you.

    Example:
      result = df.groupby('customer')['grand_total'].sum().nlargest(10).to_dict()
    """,
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python snippet that sets `result`"},
            "data": {
                "description": "The data to analyse. Paste rows from an earlier tool result.",
                "type": ["array", "object", "null"],
            },
            "purpose": {"type": "string", "description": "One line on what this computes"},
        },
        "required": ["code"],
    },
)
def analyse_data(code: str, data=None, purpose: str = ""):
    from research_agent.compat import call_safe_exec

    ns = _namespace(data)
    try:
        _locals, stdout = call_safe_exec(code, ns)
    except Exception as e:
        return {"ok": False, "purpose": purpose, "error": f"{type(e).__name__}: {e}"}

    result = (_locals or {}).get("result", ns.get("result"))
    if result is None:
        return {
            "ok": False,
            "purpose": purpose,
            "logs": stdout,
            "error": "Snippet finished but never assigned `result`. Assign the answer to result.",
        }

    try:
        import pandas as pd

        if isinstance(result, pd.DataFrame):
            result = result.to_dict(orient="records")
        elif isinstance(result, pd.Series):
            result = result.to_dict()
    except ImportError:
        pass

    payload = json.dumps(result, default=str)
    if len(payload) > MAX_OUTPUT_CHARS:
        return {
            "ok": False,
            "purpose": purpose,
            "logs": stdout,
            "error": f"Result is {len(payload)} chars, over the {MAX_OUTPUT_CHARS} limit. Aggregate further.",
        }

    return {"ok": True, "purpose": purpose, "logs": (stdout or "")[:4000], "result": json.loads(payload)}
