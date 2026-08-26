"""Visualisation tools.

These tools do not draw anything. They emit an artifact spec that the desk
page renders with frappe-charts, and that can later be pinned to an ERPNext
Workspace as a Dashboard Chart or Number Card.

Keeping rendering out of the agent means the same artifact works in the desk
UI, in an emailed PDF and over MCP in Claude Desktop, without the model
knowing about any of them.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from research_agent.agent.registry import tool

CHART_TYPES = ["bar", "line", "pie", "donut", "percentage", "axis-mixed", "heatmap"]


def _emit(context: dict, artifact: dict) -> dict:
    """Attach an artifact to the running session and push it to the browser."""
    session = context.get("session")
    artifact.setdefault("artifact_id", frappe.generate_hash(length=8))
    if session:
        session.append(
            "artifacts",
            {
                "artifact_id": artifact["artifact_id"],
                "artifact_type": artifact["type"],
                "title": artifact.get("title") or "Untitled",
                "spec": json.dumps(artifact, default=str, indent=2),
            },
        )
        session.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.publish_realtime(
            "research_agent_artifact",
            {"session": session.name, "artifact": artifact},
            user=session.owner,
        )
    return {"created": artifact["type"], "artifact_id": artifact["artifact_id"], "title": artifact.get("title")}


@tool(
    name="create_chart",
    category="Visualisation",
    description="""
    Render a chart from data you already fetched. Call this whenever the answer
    involves a comparison, a trend over time, a share of total or a ranking. Do not
    describe a chart in words instead of calling this.

    Pick the type honestly: line for time series, bar for ranking or category
    comparison, pie or donut only for parts of one whole with under 7 slices,
    percentage for a single stacked share bar.

    labels and each dataset's values must be the same length. Values must be plain
    numbers, not strings and not currency-formatted.
    """,
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "chart_type": {"type": "string", "enum": CHART_TYPES},
            "labels": {"type": "array", "items": {"type": "string"}},
            "datasets": {
                "type": "array",
                "description": "One or more series",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "values": {"type": "array", "items": {"type": "number"}},
                        "chartType": {"type": "string", "enum": ["bar", "line"], "description": "Only for axis-mixed"},
                    },
                    "required": ["name", "values"],
                },
            },
            "y_axis_label": {"type": "string"},
            "value_prefix": {"type": "string", "description": "e.g. '₹' or '$'"},
            "commentary": {"type": "string", "description": "One or two lines on what the chart shows"},
            "source": {"type": "string", "description": "Which tool call this data came from"},
        },
        "required": ["title", "chart_type", "labels", "datasets"],
    },
)
def create_chart(
    title: str,
    chart_type: str,
    labels: list,
    datasets: list,
    y_axis_label: str = "",
    value_prefix: str = "",
    commentary: str = "",
    source: str = "",
    context: dict | None = None,
):
    if chart_type not in CHART_TYPES:
        frappe.throw(_("chart_type must be one of {0}").format(", ".join(CHART_TYPES)))
    if not labels or not datasets:
        frappe.throw(_("labels and datasets cannot be empty."))

    clean = []
    for ds in datasets:
        values = [None if v is None else float(v) for v in ds.get("values", [])]
        if len(values) != len(labels):
            frappe.throw(
                _("Dataset '{0}' has {1} values but there are {2} labels.").format(
                    ds.get("name"), len(values), len(labels)
                )
            )
        entry = {"name": ds.get("name") or "Series", "values": values}
        if ds.get("chartType"):
            entry["chartType"] = ds["chartType"]
        clean.append(entry)

    return _emit(
        context or {},
        {
            "type": "chart",
            "title": title,
            "chart_type": chart_type,
            "data": {"labels": [str(x) for x in labels], "datasets": clean},
            "y_axis_label": y_axis_label,
            "value_prefix": value_prefix,
            "commentary": commentary,
            "source": source,
        },
    )


@tool(
    name="create_table",
    category="Visualisation",
    description="""
    Render a tabular artifact. Use for detail the reader will scan or export, such as
    a top-20 customer list or an ageing breakdown. Keep it under 100 rows; summarise
    instead of dumping. Mark numeric columns so they right-align and total correctly.
    """,
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "columns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                        "numeric": {"type": "boolean"},
                        "format": {"type": "string", "enum": ["currency", "number", "percent", "date", "text"]},
                    },
                    "required": ["key", "label"],
                },
            },
            "rows": {"type": "array", "items": {"type": "object"}},
            "total_row": {"type": "boolean", "description": "Add a totals row for numeric columns"},
            "commentary": {"type": "string"},
        },
        "required": ["title", "columns", "rows"],
    },
)
def create_table(
    title: str,
    columns: list,
    rows: list,
    total_row: bool = False,
    commentary: str = "",
    context: dict | None = None,
):
    return _emit(
        context or {},
        {
            "type": "table",
            "title": title,
            "columns": columns,
            "rows": rows[:200],
            "row_count": len(rows),
            "total_row": bool(total_row),
            "commentary": commentary,
        },
    )


@tool(
    name="create_metric",
    category="Visualisation",
    description="""
    Render a single headline number with an optional comparison, like a number card.
    Use for the one figure the CXO asked for. Give delta_label context such as
    'vs last quarter' so the number is not floating without a baseline.
    """,
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "value": {"type": "number"},
            "formatted_value": {"type": "string", "description": "e.g. '₹ 4.2 Cr'"},
            "delta_percent": {"type": "number"},
            "delta_label": {"type": "string"},
            "direction_is_good": {"type": "boolean", "description": "False when up is bad, e.g. DSO or returns"},
            "commentary": {"type": "string"},
        },
        "required": ["title", "value"],
    },
)
def create_metric(
    title: str,
    value: float,
    formatted_value: str = "",
    delta_percent: float | None = None,
    delta_label: str = "",
    direction_is_good: bool = True,
    commentary: str = "",
    context: dict | None = None,
):
    return _emit(
        context or {},
        {
            "type": "metric",
            "title": title,
            "value": value,
            "formatted_value": formatted_value or frappe.utils.fmt_money(value),
            "delta_percent": delta_percent,
            "delta_label": delta_label,
            "direction_is_good": direction_is_good,
            "commentary": commentary,
        },
    )


@tool(
    name="assemble_dashboard",
    category="Visualisation",
    description="""
    Arrange artifacts you already created into a dashboard layout and give it a name.
    Call this last, once, when the user asked for a dashboard or when three or more
    artifacts belong together. Reference artifacts by the artifact_id returned when
    you created them. Order matters: put the headline metrics first.
    """,
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string", "description": "Two or three lines a CXO can read alone"},
            "layout": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "width": {"type": "string", "enum": ["full", "half", "third", "quarter"]},
                    },
                    "required": ["artifact_id"],
                },
            },
        },
        "required": ["title", "layout"],
    },
)
def assemble_dashboard(title: str, layout: list, summary: str = "", context: dict | None = None):
    session = (context or {}).get("session")
    known = {a.artifact_id for a in (session.artifacts if session else [])}
    missing = [b["artifact_id"] for b in layout if b.get("artifact_id") not in known]
    if missing:
        frappe.throw(_("Unknown artifact_id: {0}. Create the artifact first.").format(", ".join(missing)))

    return _emit(
        context or {},
        {"type": "dashboard", "title": title, "summary": summary, "layout": layout},
    )
