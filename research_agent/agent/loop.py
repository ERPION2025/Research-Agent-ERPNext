"""The reflexion loop.

This is Reflexion in the Shinn et al. sense, not plain reflection. The
difference matters and is worth being precise about, because most
implementations labelled "reflexion" are actually single-pass critique:

  reflection  - draft once, critique it, rewrite once. One trajectory.
  reflexion   - run a whole trajectory, score it with an evaluator, write a
                verbal lesson into episodic memory, then run the trajectory
                again from scratch with those lessons in context.

The second one is what recovers from a bad plan. If the first trial queried
the wrong DocType, no amount of rewriting the prose will fix it; you have to
go back and query again. So the outer loop here re-plans and re-acts, and
the only thing that survives between trials is the reflection text.

    trial 1: plan -> act -> compose -> evaluate -> FAIL -> reflect
    trial 2: plan(+reflections) -> act -> compose -> evaluate -> PASS
    publish
"""

from __future__ import annotations

import json
import time

import frappe
from frappe.utils import now_datetime

from research_agent.agent import evaluator
from research_agent.agent.llm import get_llm
from research_agent.agent.registry import available_tools, envelope_to_text, execute, tool_schemas

SYSTEM = """You are a research and analytics agent embedded inside an ERPNext instance at {site}.
You answer business questions for operators and executives.

Today is {today}. You are acting as {user}, whose roles are: {roles}.
You can only see data that user is allowed to see. If a query comes back empty,
consider that it may be a permission boundary rather than an absence of data, and say so.

How to work:
1. Reach for the purpose-built operations tools first. They encode the accounting
   correctly and save you five wrong queries:
     "sales today / this week"          -> erp_sales_today
     "collection today / cash in"       -> erp_collections_today
     "how many did we make / produce"   -> erp_production_today
     "what does this model cost / COGS" -> erp_item_cost
     "how are we doing today"           -> erp_daily_pulse
   Only fall back to the generic tools when none of these fit.
2. Start with erp_company_context whenever a period or currency is involved.
3. For anything else, find the right table with erp_list_doctypes and
   erp_describe_doctype before filtering. Never guess a fieldname.
4. Prefer erp_run_report over hand-built SQL for accounting-shaped questions:
   ledgers, ageing, stock balance, gross profit.
5. Use web_search only for facts outside the company: market size, competitor
   moves, regulation, benchmarks. Never for anything the ERP knows. Check
   find_research_reports first in case someone already researched it.
6. Compute derived numbers with analyse_data, not in your head.
7. Turn the answer into artifacts. create_metric for the headline number,
   create_chart for any comparison or trend, create_table for detail,
   assemble_dashboard at the end if there are three or more.
8. After real external research, call publish_research_report so the findings
   become a permanent document linked to the item or brand. Not for routine
   internal reporting.

Changing anything:
- You cannot write to the ERP. You can only propose, using erp_propose_create,
  erp_propose_update, erp_propose_submit or erp_propose_cancel.
- A proposal creates an approval request. Nothing has changed until a human
  approves it. Never tell the user a document exists when you only proposed it.
  Say what will happen and that it is waiting for approval.
- Only propose a change when the user clearly asked for one. Answering a question
  is not a licence to act on it.

How to answer:
- Lead with the answer, then the evidence. No preamble.
- State the exact date range and company for every figure.
- Name your source for each number, e.g. "Sales Invoice, submitted only, Apr-Jun 2026".
- COGS means three different things. If you quote one, say which: current stock
  valuation, BOM cost, or realised cost on units sold.
- Sales and collections are different numbers. So are order intake and invoiced
  revenue, and Work Order completed quantity and Stock Entry production. Never
  blur them.
- If the data does not support a confident answer, say what is missing and what
  you would need. A hedged honest answer beats a confident wrong one.
- Plain business English. Short sentences. No jargon.

{reflections}"""

PLAN_PROMPT = """Question from {user}:
{question}

Write a short plan before touching any tool. Cover:
1. What exactly is being asked, including the implied period and entity.
2. Which ERPNext tables or reports are likely to hold it.
3. Whether outside-the-company research is needed at all.
4. Which artifacts the answer should end with.

Six lines maximum. No tool calls yet."""

COMPOSE_PROMPT = """Write the final answer now, using only what your tool calls returned.

Reminders:
- Lead with the direct answer.
- Every figure needs its source and date range.
- Reference the artifacts you created by title, do not re-describe their contents in full.
- If something is uncertain or incomplete, say so plainly.

Markdown. No heading above level 3. No closing pleasantries."""

REFLECT_PROMPT = """Your attempt was evaluated and did not pass.

The question was:
{question}

What you produced:
{draft}

Tools you called, in order:
{tool_log}

Where it fell short:
{critique}

Write a reflection for your next attempt. Be concrete and operational, not
encouraging. Name the specific tool call you should have made, the specific field
or filter you got wrong, the specific step you skipped. If your plan itself was
wrong, say what the plan should have been instead.

Six lines maximum. Write it as instructions to yourself, starting each line with a verb."""


class ReflexionAgent:
    def __init__(self, session_name: str):
        self.session = frappe.get_doc("Research Session", session_name)
        self.settings = frappe.get_cached_doc("Research Agent Settings")
        self.reflections: list[str] = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.tools = available_tools(
            include_web=bool(self.settings.allow_web_search),
            include_mcp=bool(self.settings.enable_mcp_client),
        )
        self.schemas = tool_schemas(self.tools)

    # ---------------------------------------------------------------- utils
    def _status(self, status: str, note: str = ""):
        self.session.db_set("status", status, update_modified=True)
        frappe.db.commit()
        frappe.publish_realtime(
            "research_agent_update",
            {"session": self.session.name, "status": status, "note": note},
            user=self.session.owner,
        )

    def _log(self, step_type: str, content: str = "", tool: str = "", arguments=None, output=None,
             trial: int = 1, ms: int = 0):
        self.session.append(
            "steps",
            {
                "trial": trial,
                "step_type": step_type,
                "tool_name": tool,
                "content": (content or "")[:140000],
                "arguments": json.dumps(arguments, default=str, indent=2)[:140000] if arguments else "",
                "output": json.dumps(output, default=str, indent=2)[:140000] if output is not None else "",
                "duration_ms": ms,
                "timestamp": now_datetime(),
            },
        )
        self.session.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.publish_realtime(
            "research_agent_step",
            {
                "session": self.session.name,
                "trial": trial,
                "step_type": step_type,
                "tool_name": tool,
                "content": content[:4000],
                "arguments": arguments,
            },
            user=self.session.owner,
        )

    def _system(self) -> str:
        block = ""
        if self.reflections:
            block = (
                "Lessons from your earlier attempts at this same question. "
                "Apply them, do not repeat those mistakes:\n\n"
                + "\n\n".join(f"Attempt {i + 1}:\n{r}" for i, r in enumerate(self.reflections))
            )
        return SYSTEM.format(
            site=frappe.local.site,
            today=frappe.utils.today(),
            user=self.session.owner,
            roles=", ".join(frappe.get_roles(self.session.owner)[:15]),
            reflections=block,
        )

    def _track(self, resp):
        self.tokens_in += resp.input_tokens
        self.tokens_out += resp.output_tokens

    # ---------------------------------------------------------------- stages
    def plan(self, trial: int) -> str:
        self._status("Planning")
        llm = get_llm(role="planner", temperature=0.1)
        resp = llm.chat(
            messages=[{"role": "user", "content": PLAN_PROMPT.format(
                user=self.session.owner, question=self.session.prompt)}],
            system=self._system(),
            max_tokens=900,
        )
        self._track(resp)
        plan = resp.content or "No plan returned."
        self._log("Plan", content=plan, trial=trial)
        return plan

    def act(self, plan: str, trial: int) -> list[dict]:
        """Tool-calling loop. Returns the transcript of tool calls."""
        self._status("Researching")
        llm = get_llm(role="worker", temperature=0.2)
        messages = [
            {"role": "user", "content": f"Question:\n{self.session.prompt}\n\nYour plan:\n{plan}\n\n"
                                        f"Execute it now using the tools. Do not write the final answer yet."}
        ]
        transcript: list[dict] = []
        max_steps = int(self.settings.max_tool_calls or 18)
        context = {"session": self.session, "trial": trial}

        for _ in range(max_steps):
            resp = llm.chat(messages=messages, tools=self.schemas, system=self._system(), max_tokens=3000)
            self._track(resp)

            if not resp.wants_tools:
                if resp.content:
                    self._log("Note", content=resp.content, trial=trial)
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in resp.tool_calls
                    ],
                }
            )

            for tc in resp.tool_calls:
                started = time.time()
                result = execute(tc.name, tc.arguments, context=context)
                ms = int((time.time() - started) * 1000)
                transcript.append(
                    {"tool": tc.name, "arguments": tc.arguments, "output": result, "trial": trial}
                )
                self._log("Tool Call", tool=tc.name, arguments=tc.arguments, output=result, trial=trial, ms=ms)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": envelope_to_text(result)}
                )
            # reload so artifacts appended by visualisation tools are visible
            self.session.reload()

        return transcript

    def compose(self, transcript: list[dict], trial: int) -> str:
        self._status("Drafting")
        llm = get_llm(role="worker", temperature=0.3)
        evidence = json.dumps(
            [{"tool": s["tool"], "arguments": s["arguments"], "result": s["output"]} for s in transcript],
            default=str,
        )[:60000]
        artifacts = [
            {"artifact_id": a.artifact_id, "type": a.artifact_type, "title": a.title}
            for a in self.session.artifacts
        ]
        resp = llm.chat(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Question:\n{self.session.prompt}\n\n"
                        f"Everything your tools returned:\n{evidence}\n\n"
                        f"Artifacts you created:\n{json.dumps(artifacts)}\n\n{COMPOSE_PROMPT}"
                    ),
                }
            ],
            system=self._system(),
            max_tokens=3000,
        )
        self._track(resp)
        draft = resp.content or ""
        self._log("Draft", content=draft, trial=trial)
        return draft

    def reflect(self, draft: str, verdict, transcript: list[dict], trial: int) -> str:
        self._status("Reflecting")
        llm = get_llm(role="reflector", temperature=0.4)
        tool_log = "\n".join(
            f"{i + 1}. {s['tool']}({json.dumps(s['arguments'], default=str)[:200]}) -> "
            f"{'ok' if s['output'].get('ok') else 'FAILED: ' + str(s['output'].get('error'))[:150]}"
            for i, s in enumerate(transcript)
        ) or "no tools were called at all"

        resp = llm.chat(
            messages=[
                {
                    "role": "user",
                    "content": REFLECT_PROMPT.format(
                        question=self.session.prompt,
                        draft=draft[:6000],
                        tool_log=tool_log[:6000],
                        critique=verdict.critique_text(),
                    ),
                }
            ],
            max_tokens=800,
        )
        self._track(resp)
        reflection = resp.content or ""
        self._log("Reflection", content=reflection, trial=trial)
        return reflection

    # ---------------------------------------------------------------- driver
    def run(self) -> dict:
        started = time.time()
        max_trials = max(1, int(self.settings.max_trials or 2))
        threshold = float(self.settings.pass_threshold or 0.75)
        best = {"draft": "", "verdict": None, "score": -1.0}

        try:
            for trial in range(1, max_trials + 1):
                self.session.db_set("current_trial", trial, update_modified=False)
                plan = self.plan(trial)
                transcript = self.act(plan, trial)
                draft = self.compose(transcript, trial)

                self._status("Evaluating")
                verdict = evaluator.evaluate(
                    question=self.session.prompt,
                    draft=draft,
                    transcript=transcript,
                    artifacts=list(self.session.artifacts),
                    threshold=threshold,
                    use_judge=bool(self.settings.use_llm_judge),
                )
                self._log("Evaluation", content=verdict.critique_text(), output=verdict.to_dict(), trial=trial)

                if verdict.score > best["score"]:
                    best = {"draft": draft, "verdict": verdict, "score": verdict.score}

                if verdict.passed or trial == max_trials:
                    break

                self.reflections.append(self.reflect(draft, verdict, transcript, trial))

            verdict = best["verdict"]
            self.session.reload()
            self.session.final_answer = best["draft"]
            self.session.eval_score = round(best["score"], 3)
            self.session.eval_detail = json.dumps(verdict.to_dict(), indent=2) if verdict else ""
            self.session.reflections = "\n\n---\n\n".join(self.reflections)
            self.session.trials_used = self.session.current_trial
            self.session.input_tokens = self.tokens_in
            self.session.output_tokens = self.tokens_out
            self.session.duration_seconds = round(time.time() - started, 1)
            self.session.status = "Completed"
            self.session.save(ignore_permissions=True)
            frappe.db.commit()

            frappe.publish_realtime(
                "research_agent_complete",
                {
                    "session": self.session.name,
                    "answer": best["draft"],
                    "score": best["score"],
                    "artifacts": [json.loads(a.spec) for a in self.session.artifacts if a.spec],
                },
                user=self.session.owner,
            )
            return {"status": "Completed", "session": self.session.name, "score": best["score"]}

        except Exception as e:
            frappe.db.rollback()
            self.session.reload()
            self.session.status = "Failed"
            self.session.error_log = frappe.get_traceback()
            self.session.save(ignore_permissions=True)
            frappe.db.commit()
            frappe.publish_realtime(
                "research_agent_update",
                {"session": self.session.name, "status": "Failed", "note": str(e)},
                user=self.session.owner,
            )
            raise


def run_session(session_name: str):
    """Entry point for frappe.enqueue."""
    return ReflexionAgent(session_name).run()
