// Copyright (c) 2026, ERPion Technologies LLP and contributors
//
// Two panes. Left is the answer and its artifacts, which is what the reader
// came for. Right is the trace rail: every plan, tool call, evaluation and
// reflection as it happens, grouped by trial. The rail is the point of the
// page. An analytics answer nobody can audit is worth nothing to a CFO, so
// the work is shown by default rather than hidden behind a toggle.
//
// Named research-agent-workbench, not research-agent: the Workspace is
// already named "Research Agent", and a Workspace and a Page with the same
// route both resolve to /app/research-agent. The workspace always wins that
// collision, so the ask box below was silently unreachable.

// The final answer is markdown composed by the LLM from tool results, which
// can include web page content (search results, extracted pages) and ERP
// field values that the agent quotes verbatim. Both are untrusted text as
// far as the browser is concerned: a competitor's page or a record entered
// by any user could contain a script or an event-handler attribute. Markdown
// conversion does not strip embedded HTML, so the rendered output is
// stripped of anything executable before it ever reaches innerHTML.
function sanitize_html(html) {
	const doc = new DOMParser().parseFromString(html, "text/html");
	["script", "style", "iframe", "object", "embed", "link", "meta", "base", "form"].forEach((tag) =>
		doc.querySelectorAll(tag).forEach((el) => el.remove())
	);
	doc.querySelectorAll("*").forEach((el) => {
		[...el.attributes].forEach((attr) => {
			const name = attr.name.toLowerCase();
			const value = attr.value.trim();
			if (name.startsWith("on")) {
				el.removeAttribute(attr.name);
			} else if ((name === "href" || name === "src") && /^\s*javascript:/i.test(value)) {
				el.removeAttribute(attr.name);
			}
		});
	});
	return doc.body.innerHTML;
}

frappe.pages["research-agent-workbench"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Research Agent"),
		single_column: true,
	});
	new ResearchAgentUI(page);
};

class ResearchAgentUI {
	constructor(page) {
		this.page = page;
		this.session = null;
		this.artifacts = {};
		this.charts = {};
		this.render_shell();
		this.bind_realtime();
		this.load_history();
	}

	render_shell() {
		this.page.main.html(`
			<div class="ra-root">
				<div class="ra-ask">
					<textarea class="form-control ra-prompt" rows="3"
						placeholder="${__("Ask about your ERP data, the market, or both. For example: compare our Q1 revenue by territory against last year, and check what the two-wheeler resale market did in the same period.")}"></textarea>
					<div class="ra-ask-footer">
						<span class="ra-hint text-muted small">${__("Cmd or Ctrl + Enter to run")}</span>
						<button class="btn btn-primary btn-sm ra-run">${__("Run research")}</button>
					</div>
				</div>

				<div class="ra-body">
					<div class="ra-answer-col">
						<div class="ra-empty text-muted">
							${__("Nothing yet. Ask a question above and the answer, charts and full working will appear here.")}
						</div>
						<div class="ra-answer hidden"></div>
						<div class="ra-sources hidden"></div>
						<div class="ra-artifacts"></div>
					</div>
					<div class="ra-trace-col">
						<div class="ra-trace-head">
							<span class="ra-status-dot"></span>
							<span class="ra-status-text">${__("Idle")}</span>
							<span class="ra-score"></span>
						</div>
						<div class="ra-trace"></div>
					</div>
				</div>
			</div>
		`);

		this.$prompt = this.page.main.find(".ra-prompt");
		this.$trace = this.page.main.find(".ra-trace");
		this.$answer = this.page.main.find(".ra-answer");
		this.$sources = this.page.main.find(".ra-sources");
		this.$artifacts = this.page.main.find(".ra-artifacts");
		this.$status = this.page.main.find(".ra-status-text");
		this.$dot = this.page.main.find(".ra-status-dot");
		this.$score = this.page.main.find(".ra-score");

		this.page.main.find(".ra-run").on("click", () => this.run());
		this.$prompt.on("keydown", (e) => {
			if ((e.metaKey || e.ctrlKey) && e.key === "Enter") this.run();
		});

		this.page.set_secondary_action(__("Settings"), () =>
			frappe.set_route("Form", "Research Agent Settings")
		);
		this.history_menu = this.page.add_menu_item(__("Recent sessions"), () => this.show_history());
		this.page.add_menu_item(__("MCP servers"), () => frappe.set_route("List", "MCP Server"));
		this.page.add_menu_item(__("Tool registry"), () => frappe.set_route("List", "Agent Tool"));
	}

	// ---------------------------------------------------------------- running
	run() {
		const prompt = (this.$prompt.val() || "").trim();
		if (!prompt) {
			frappe.show_alert({ message: __("Ask a question first"), indicator: "orange" });
			return;
		}
		this.reset();
		this.set_status("Queued");
		frappe.call({
			method: "research_agent.api.start_session",
			args: { prompt },
			callback: (r) => {
				this.session = r.message.session;
				this.page.set_indicator(this.session, "blue");
			},
			error: () => this.set_status("Failed"),
		});
	}

	reset() {
		this.artifacts = {};
		this.citations = {};
		this.$sources.addClass("hidden").empty();
		Object.values(this.charts).forEach((c) => c && c.destroy && c.destroy());
		this.charts = {};
		this.$trace.empty();
		this.$answer.addClass("hidden").empty();
		this.$artifacts.empty();
		this.$score.text("");
		this.page.main.find(".ra-empty").addClass("hidden");
	}

	set_status(status, note) {
		const running = !["Completed", "Failed", "Cancelled", "Idle"].includes(status);
		this.$status.text(note ? `${__(status)} - ${note}` : __(status));
		this.$dot
			.removeClass("running done failed")
			.addClass(running ? "running" : status === "Failed" ? "failed" : "done");
	}

	// -------------------------------------------------------------- realtime
	bind_realtime() {
		const mine = (d) => !this.session || d.session === this.session;

		frappe.realtime.on("research_agent_update", (d) => {
			if (mine(d)) this.set_status(d.status, d.note);
		});

		frappe.realtime.on("research_agent_step", (d) => {
			if (mine(d)) this.add_step(d);
		});

		frappe.realtime.on("research_agent_artifact", (d) => {
			if (mine(d)) this.add_artifact(d.artifact);
		});

		frappe.realtime.on("research_agent_complete", (d) => {
			if (!mine(d)) return;
			this.set_status("Completed");
			this.show_answer(d.answer, d.score, d.citations);
		});
	}

	// ----------------------------------------------------------------- trace
	add_step(step) {
		const icons = {
			Plan: "list",
			"Tool Call": "tool",
			Note: "message",
			Draft: "edit",
			Evaluation: "check",
			Reflection: "refresh",
		};
		const label =
			step.step_type === "Tool Call" ? step.tool_name : step.step_type;

		const $trial = this.trial_group(step.trial);
		const $row = $(`
			<div class="ra-step ra-step-${frappe.scrub(step.step_type)}">
				<div class="ra-step-head">
					${frappe.utils.icon(icons[step.step_type] || "small-message", "sm")}
					<span class="ra-step-label">${frappe.utils.escape_html(label || "")}</span>
					<span class="ra-step-toggle">+</span>
				</div>
				<div class="ra-step-detail hidden"></div>
			</div>
		`);

		const detail =
			step.step_type === "Tool Call"
				? JSON.stringify(step.arguments, null, 2)
				: step.content || "";
		$row.find(".ra-step-detail").text(detail);
		$row.find(".ra-step-head").on("click", () => {
			const $d = $row.find(".ra-step-detail");
			$d.toggleClass("hidden");
			$row.find(".ra-step-toggle").text($d.hasClass("hidden") ? "+" : "−");
		});

		$trial.append($row);
		this.$trace.scrollTop(this.$trace[0].scrollHeight);
	}

	trial_group(trial) {
		let $g = this.$trace.find(`[data-trial="${trial}"]`);
		if (!$g.length) {
			const suffix =
				trial > 1
					? `<span class="ra-retry">${__("after reflection")}</span>`
					: "";
			this.$trace.append(`
				<div class="ra-trial" data-trial="${trial}">
					<div class="ra-trial-head">${__("Trial {0}", [trial])} ${suffix}</div>
				</div>
			`);
			$g = this.$trace.find(`[data-trial="${trial}"]`);
		}
		return $g;
	}

	// ------------------------------------------------------------- artifacts
	add_artifact(a) {
		this.artifacts[a.artifact_id] = a;
		if (a.type === "dashboard") return this.render_dashboard(a);

		const $card = $(`<div class="ra-card" data-id="${a.artifact_id}"></div>`);
		this.$artifacts.append($card);
		this.render_into($card, a);
	}

	render_into($card, a) {
		$card.append(`<div class="ra-card-title">${frappe.utils.escape_html(a.title || "")}</div>`);

		if (a.type === "metric") {
			const good = a.direction_is_good !== false;
			const up = (a.delta_percent || 0) >= 0;
			const cls = a.delta_percent == null ? "" : (up === good ? "up" : "down");
			$card.append(`
				<div class="ra-metric">
					<div class="ra-metric-value">${frappe.utils.escape_html(a.formatted_value || a.value)}</div>
					${
						a.delta_percent == null
							? ""
							: `<div class="ra-metric-delta ${cls}">${up ? "▲" : "▼"} ${Math.abs(
									a.delta_percent
							  ).toFixed(1)}% <span>${frappe.utils.escape_html(a.delta_label || "")}</span></div>`
					}
				</div>
			`);
		} else if (a.type === "chart") {
			const $holder = $('<div class="ra-chart"></div>').appendTo($card);
			this.charts[a.artifact_id] = new frappe.Chart($holder[0], {
				data: a.data,
				type: a.chart_type === "donut" ? "donut" : a.chart_type,
				height: 260,
				axisOptions: { xIsSeries: a.chart_type === "line" },
				tooltipOptions: {
					formatTooltipY: (d) =>
						`${a.value_prefix || ""}${frappe.format(d, { fieldtype: "Float" })}`,
				},
				colors: ["#2490ef", "#48bb74", "#f6c000", "#ff5858", "#7c3aed", "#0ea5e9"],
			});
		} else if (a.type === "table") {
			$card.append(this.table_html(a));
		}

		if (a.commentary) {
			$card.append(
				`<div class="ra-card-note text-muted">${frappe.utils.escape_html(a.commentary)}</div>`
			);
		}
		if (a.type === "chart") {
			$card.append(
				`<button class="btn btn-xs btn-default ra-pin">${__("Pin to workspace")}</button>`
			);
			$card.find(".ra-pin").on("click", () => this.pin(a.artifact_id));
		}
	}

	table_html(a) {
		const head = a.columns.map((c) => `<th class="${c.numeric ? "num" : ""}">${frappe.utils.escape_html(c.label)}</th>`).join("");
		const body = a.rows
			.map(
				(r) =>
					"<tr>" +
					a.columns
						.map((c) => {
							const v = r[c.key];
							const shown =
								c.format === "currency" && v != null
									? frappe.format(v, { fieldtype: "Currency" })
									: v == null
									? ""
									: String(v);
							return `<td class="${c.numeric ? "num" : ""}">${frappe.utils.escape_html(shown)}</td>`;
						})
						.join("") +
					"</tr>"
			)
			.join("");
		const more =
			a.row_count > a.rows.length
				? `<div class="text-muted small">${__("Showing {0} of {1} rows", [a.rows.length, a.row_count])}</div>`
				: "";
		return `<div class="ra-table-wrap"><table class="table table-sm ra-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${more}</div>`;
	}

	render_dashboard(d) {
		const $wrap = $(`<div class="ra-dashboard"></div>`);
		$wrap.append(`<div class="ra-dash-title">${frappe.utils.escape_html(d.title)}</div>`);
		if (d.summary) $wrap.append(`<div class="ra-dash-summary">${frappe.utils.escape_html(d.summary)}</div>`);
		const $grid = $('<div class="ra-grid"></div>').appendTo($wrap);

		d.layout.forEach((block) => {
			const a = this.artifacts[block.artifact_id];
			if (!a) return;
			const $existing = this.$artifacts.find(`[data-id="${block.artifact_id}"]`);
			if ($existing.length) $existing.remove();
			const $card = $(`<div class="ra-card ra-w-${block.width || "half"}" data-id="${a.artifact_id}"></div>`);
			$grid.append($card);
			this.render_into($card, a);
		});
		this.$artifacts.prepend($wrap);
	}

	pin(artifact_id) {
		frappe.prompt(
			{ fieldname: "workspace", label: __("Workspace"), fieldtype: "Link", options: "Workspace", reqd: 1 },
			(v) =>
				frappe.call({
					method: "research_agent.api.pin_to_workspace",
					args: { session: this.session, workspace: v.workspace, artifact_id },
					callback: () =>
						frappe.show_alert({ message: __("Pinned"), indicator: "green" }),
				}),
			__("Pin chart to workspace")
		);
	}

	// -------------------------------------------------------------- answer
	show_answer(md, score, citations) {
		const html = sanitize_html(frappe.markdown(md || ""));
		this.$answer.removeClass("hidden").html(this.linkify_citations(html, citations || []));
		this.render_sources(citations || []);
		if (score != null) {
			const colour = score >= 0.85 ? "green" : score >= 0.7 ? "orange" : "red";
			this.$score.html(
				`<span class="indicator-pill ${colour}">${__("score")} ${score.toFixed(2)}</span>`
			);
		}
	}

	// [D1] markers are written by the model as plain text; code, not the
	// model, decides what they link to. A marker with no matching citation
	// is shown struck through rather than silently dropped, since that is a
	// fabricated citation and hiding it would defeat the audit trail.
	linkify_citations(html, citations) {
		const by_ref = {};
		citations.forEach((c) => (by_ref[c.ref] = c));
		return html.replace(/\[(D\d{1,2})\]/g, (match, ref) => {
			const c = by_ref[ref];
			if (!c) {
				return `<span class="ra-cite broken" title="${__(
					"This marker matches no retrieved passage."
				)}">${frappe.utils.escape_html(ref)}</span>`;
			}
			const title = [c.file, c.page ? __("p. {0}", [c.page]) : null].filter(Boolean).join(" · ");
			return `<a class="ra-cite" href="${frappe.utils.escape_html(c.url || "#")}" target="_blank" rel="noopener" title="${frappe.utils.escape_html(title)}">${frappe.utils.escape_html(ref)}</a>`;
		});
	}

	render_sources(citations) {
		if (!citations.length) {
			this.$sources.addClass("hidden").empty();
			return;
		}
		const rows = citations
			.map((c) => {
				const loc = [c.section, c.page ? __("p. {0}", [c.page]) : null].filter(Boolean).join(" · ");
				return `
					<div class="ra-source">
						<div class="ra-source-ref">${frappe.utils.escape_html(c.ref)}</div>
						<div class="ra-source-body">
							<a href="${frappe.utils.escape_html(c.url || "#")}" target="_blank" rel="noopener">${frappe.utils.escape_html(c.file || "")}</a>
							${loc ? `<span class="ra-source-loc">${frappe.utils.escape_html(loc)}</span>` : ""}
							${c.snippet ? `<div class="ra-source-snip">${frappe.utils.escape_html(c.snippet)}</div>` : ""}
						</div>
					</div>`;
			})
			.join("");
		this.$sources.removeClass("hidden").html(`<div class="ra-sources-head">${__("Sources")}</div>${rows}`);
	}

	// -------------------------------------------------------------- history
	load_history() {
		frappe.call("research_agent.api.recent_sessions").then((r) => (this.history = r.message || []));
	}

	show_history() {
		const d = new frappe.ui.Dialog({ title: __("Recent sessions"), size: "large" });
		const rows = (this.history || [])
			.map(
				(s) =>
					`<tr><td><a data-name="${s.name}" class="ra-hist">${frappe.utils.escape_html(s.title)}</a></td>
					 <td>${s.status}</td><td>${s.eval_score ? s.eval_score.toFixed(2) : "-"}</td>
					 <td class="text-muted">${frappe.datetime.comment_when(s.creation)}</td></tr>`
			)
			.join("");
		d.$body.html(`<table class="table table-sm"><tbody>${rows || "<tr><td>-</td></tr>"}</tbody></table>`);
		d.$body.find(".ra-hist").on("click", (e) => {
			d.hide();
			this.open($(e.currentTarget).data("name"));
		});
		d.show();
	}

	open(name) {
		this.reset();
		this.session = name;
		frappe.call({ method: "research_agent.api.get_session", args: { name } }).then((r) => {
			const s = r.message;
			this.$prompt.val(s.prompt);
			this.set_status(s.status);
			(s.steps || []).forEach((st) => this.add_step(st));
			(s.artifacts || []).forEach((a) => this.add_artifact(a));
			if (s.final_answer) this.show_answer(s.final_answer, s.eval_score, s.citations);
		});
	}
}
