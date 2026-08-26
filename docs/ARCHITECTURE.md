# Architecture

## Request flow

```
Desk page                api.py            worker queue          agent/loop.py
    |                       |                    |                     |
    |-- start_session ----->|                    |                     |
    |                       |-- insert Session ->|                     |
    |                       |-- enqueue -------->|                     |
    |<-- {session: RS-...} -|                    |-- run_session ----->|
    |                                                                  |
    |<========== realtime: research_agent_step / _artifact ============|
    |<========== realtime: research_agent_complete ====================|
```

Everything after `start_session` is push. The page never polls. `get_session`
exists only for reopening history and for recovering if a socket drops.

The job runs on the `long` queue with a 30 minute timeout. A two-trial run
with the judge on is typically 40 to 120 seconds depending on how many tool
calls the plan needs.

---

## The loop

```
for trial in 1..max_trials:

    plan      planner model, no tools, six lines
                |
    act       worker model, tool loop, up to max_tool_calls
                |   each call -> registry.execute -> Research Step row
                |                                 -> realtime push
                |   visualisation calls also append Research Artifact rows
                |
    compose   worker model, sees the whole transcript, writes markdown
                |
    evaluate  4 code checks + 1 LLM judge, weighted
                |
             pass? -> break
                |
    reflect   reflector model, reads the critique and the tool log,
              writes operational instructions to itself
                |
              appended to self.reflections, injected into the system
              prompt for the next trial

publish best-scoring draft, not necessarily the last one
```

Two details that matter:

**The best draft wins, not the last.** Trial two can score worse than trial
one. The loop keeps the highest-scoring draft rather than assuming monotonic
improvement, because reflexion sometimes overcorrects.

**Artifacts survive across trials.** They are appended to the session as they
are created, so a chart made in trial one is still on screen if trial two
fails to make one. This is a deliberate trade: it means an abandoned trial can
leave an orphan chart. `assemble_dashboard` only references artifact ids that
exist, so the final layout stays coherent.

---

## Permission model

Three layers, in the order they fire:

```
1. Research Agent Settings allowlist
   |  a ceiling, never a grant
   v
2. frappe.has_permission(doctype, "read")
   |  the calling user, not a service account
   v
3. frappe.get_list
   |  applies User Permissions, share rules, permission queries
   v
   rows
```

Hand-written SQL does not go through `get_list`, so it does not automatically
carry User Permissions the way a `get_list` call does; `frappe.has_permission`
on its own only proves doctype-level access, not that these particular rows
are the user's to see. Two things handle that gap:

- `erp_read_query` (off by default) carries its own validation on top of
  `_check_doctype`: single statement, `SELECT` or `WITH` only, a
  blocked-keyword regex, a table extraction pass — and it refuses to run at
  all for any user who has a User Permission record, since a hand-written
  query cannot have that restriction verified against it generically. Such a
  user is pointed at `erp_fetch_records` and `erp_run_report` instead.
- The operational tools in `erpnext_ops.py` drop to SQL for joins `get_list`
  cannot express (Sales Invoice Item, Sales Team, Journal Entry Account,
  Stock Entry Detail). Each of those queries is scoped with `_permitted_names`,
  which asks `get_list` for the primary document names the user may see under
  the same filters, then restricts the join to `... where x.name in %(names)s`.
  That reuses the real permission engine rather than re-implementing it, and
  is what keeps a Territory-restricted user's sales breakdown restricted.

There is nowhere in the codebase that calls `ignore_permissions=True` on a
data read. The only `ignore_permissions` calls are on the session and artifact
records the agent writes about its own run, and on registry rows during
install.

The MCP server endpoint inherits all of this because it resolves a real
ERPNext user from the Frappe API key before dispatching to the same
`registry.execute`.

---

## Why three model slots

| Slot | Job | Typical model | Why |
|---|---|---|---|
| planner | decide the approach | `o4-mini` | Wrong plans are expensive; reasoning pays for itself once |
| worker | run tools, write draft | `gpt-4.1-mini` | Called 10 to 20 times per run, needs to be cheap and fast |
| reflector | judge and reflect | `gpt-4.1` | Two calls per failed trial; needs to actually spot the flaw |

Putting the strong model on the worker loop is the most common way to burn
budget for no accuracy gain. The worker is following a plan and calling
documented tools. That is not the hard part.

---

## Evaluator weights

| Check | Weight | What it catches |
|---|---|---|
| grounding | 0.30 | Numbers in the prose that appear in no tool output |
| erp_grounded | 0.25 | A question about "our revenue" answered without querying the ERP |
| artifacts | 0.15 | "Show me a chart" answered with a paragraph |
| source_quality | 0.10 | Web answers built on content farms |
| llm_judge | 0.20 | Completeness, decision usefulness, honesty about gaps |

The four code checks run first. If they collectively score under 0.4 the judge
is skipped, because there is no point paying for a nuanced critique of an
answer that never queried anything.

The scores are not what drives the retry. The *evidence sentences* are. A
model told "you scored 0.42" learns nothing; a model told "three of five
figures do not appear in any tool output" knows exactly what to do.

---

## Artifact spec

One shape, four types, rendered by whoever is displaying it:

```json
{
  "artifact_id": "a1b2c3d4",
  "type": "chart",
  "title": "Revenue by territory, Apr-Jun 2026",
  "chart_type": "bar",
  "data": { "labels": ["West", "South"], "datasets": [{"name": "Revenue", "values": [4200000, 3100000]}] },
  "value_prefix": "₹",
  "commentary": "West carries 58% of the quarter.",
  "source": "Sales Invoice, submitted, posting_date Apr-Jun 2026"
}
```

The agent never draws. It emits specs. The same spec renders in the desk page
today, and could render in an emailed PDF or a Frappe UI portal tomorrow
without the model knowing either exists. `pin_to_workspace` converts a chart
spec into a real ERPNext Dashboard Chart, which is the bridge from a one-off
question to a permanent report.

---

## Extending

**New data source.** Write a tool in `agent/tools/`, decorate it, import it in
`registry._load_builtins`. It shows up everywhere: the agent, the MCP server,
the Agent Tool registry.

**New provider.** Subclass `BaseProvider` in `agent/llm.py`, translate to and
from the OpenAI message shape, add it to `PROVIDERS` and to the Select options
on the settings doctype.

**New evaluator check.** Add a function returning a `Check` in
`agent/evaluator.py` and append it in `evaluate`. Weights are relative, they do
not need to sum to 1.

**New artifact type.** Add the emitter in `tools/visualize.py`, add a branch
in `render_into` in the page JS, add the type to the Select on Research
Artifact.

---

## Version compatibility (v15 and v16)

Everything that differs between Frappe 15 and 16 lives in `compat.py`, and
almost all of it is a feature probe rather than a version comparison. Version
strings lie on develop branches and on forks; `inspect.signature` does not.

| Concern | How it is handled |
|---|---|
| `frappe.get_list` argument drift (`ignore_ifnull`) | `safe_get_list` inspects the signature and drops unsupported kwargs |
| `safe_exec` returning 2 or 3 values, and gaining `restrict_commit_rollback` | `call_safe_exec` inspects and adapts, returns `(locals, stdout)` |
| `Report.get_data` gaining `ignore_prepared_report` / `are_default_filters` | `run_report` inspects and passes only what exists |
| Renamed fields between ERPNext versions | `pick_field(doctype, *candidates)` returns the first that exists |
| Optional modules (Manufacturing, POS) | `has_doctype` before touching Work Order, Job Card, POS Invoice |

The rule when adding code: never write `if FRAPPE_MAJOR >= 16`. Probe for the
thing you need. The only place a version number is used directly is the
diagnostics panel on the settings form.

---

## Write path

```
agent                    Agent Action Request              executor
  |                              |                             |
  |-- erp_propose_create ------->|                             |
  |   _guard(): settings on?     |                             |
  |             hard-blocklist?  |                             |
  |             user has perm?   |                             |
  |             doctype allowed? |                             |
  |                              |-- insert, Pending Approval  |
  |                              |-- _auto_approve() ?         |
  |                              |        no  -> notify        |
  |                              |        yes -------------->  |
  |<-- {request, status} --------|                             |
                                 |                             |
   human clicks Approve -------->|--------------------------->|
                                 |                   frappe.set_user(req.owner)
                                 |                   doc.insert() / save() / submit()
                                 |<-- Executed, result_docname |
```

`_guard` runs four checks before a request is even created, in this order:
write actions enabled, doctype not in `HARD_BLOCKED`, doctype not in the
configured blocklist, doctype on the writable list if one is set, and finally
`frappe.has_permission` for the requesting user. The permission check is last
because it is the most expensive, but it is never skipped.

`HARD_BLOCKED` is a module constant, not a setting. User, Role, Custom Field,
Server Script, Property Setter, System Settings, Webhook and the app's own
configuration doctypes cannot be written by the agent under any configuration.
A privilege-escalation path should not be one checkbox away.

Execution happens as `req.owner` via `frappe.set_user`, wrapped in
try/except/finally so the session user is always restored even on failure. On
exception the transaction rolls back and the request is marked Failed with the
traceback attached.

---

## Research Session vs Market Research Report

Two documents, two lifetimes, on purpose.

| | Research Session | Market Research Report |
|---|---|---|
| What it is | A conversation and its tool log | A finding of record |
| Lifetime | Purged after 90 days | Permanent, submittable, amendable |
| Owner | The person who asked | The business |
| Links | None | Item, Item Group, Brand, Territory, Customer Group, Supplier Group |
| Found via | The agent's history menu | The Item dashboard, list view, reports |
| Contains | Steps, arguments, raw tool output, reflections | Summary, findings with sources, competitor observations, recommendations |

The session is working memory. The report is the record. `publish_research_report`
is the bridge, and it sets `research_session` on the report and `research_report`
on the session so the audit trail runs both ways.

Quality scores on the report are derived in `validate`, never typed. Someone
editing a finding cannot leave a stale confidence score behind.
