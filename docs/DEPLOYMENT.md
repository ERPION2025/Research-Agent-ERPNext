# Deployment and setup

## First, the thing that trips people up

Research Agent does not "connect to" an ERPNext account. There is no OAuth screen, no API key you paste into a third-party dashboard, no external service that reaches into your ERP.

It is a Frappe app. It installs **into** the bench, alongside `frappe` and `erpnext`, and runs inside the same Python process as the rest of your site. That is what makes the permission model work: `frappe.get_list` called from inside the app is the same call the desk makes, so User Permissions, share rules and permission queries apply for free. An external service reaching in over the REST API could not do that without either a service account or a permission-replication layer, and both of those are how ERP integrations leak data.

```
        your server / Frappe Cloud bench
   ┌───────────────────────────────────────────┐
   │  frappe-bench                             │
   │   ├── apps/frappe                         │
   │   ├── apps/erpnext                        │
   │   └── apps/research_agent   ← installs here
   │                                           │
   │  sites/erp.acme.com                       │
   │   └── site_config.json (encryption_key)   │
   │        └── API keys stored encrypted      │
   └───────────────────────────────────────────┘
                    │
        outbound HTTPS only, per query
                    ▼
     api.openai.com / api.anthropic.com / api.tavily.com
```

### What actually leaves your network

Only two things, and only while a query is running:

1. **LLM API calls.** The prompt, the plan, and the tool results the agent decided to keep. That means **rows of your ERP data are in the prompt body.** Invoice totals, customer names, item costs. This is not a footnote, it is the main data-governance question and you should answer it before rollout, not after.
2. **Tavily search calls.** The search query text only. No ERP data.

Nothing is stored outside your site. No telemetry, no phone-home, no vendor dashboard. Sessions, artifacts, reports and action requests are all rows in your own database.

If sending ERP rows to a US-hosted API is not acceptable for a given client, see [Regulated deployments](#regulated-deployments) below.

---

## Path A — Frappe Cloud

Frappe Cloud allows custom apps **only on private benches**, so a site on a shared public bench cannot install this. That is a platform rule, not something the app controls.

### 1. Get the code onto GitHub

```bash
cd ~/frappe-bench/apps/research_agent
git init && git add . && git commit -m "Research Agent v0.1.0"
git remote add origin https://github.com/YOURORG/research_agent.git
git push -u origin main
```

Private repos work. Note that Frappe Cloud gives SSH access to private bench groups, so anyone with SSH on that bench can read your source regardless of repo visibility. Plan licensing accordingly.

### 2. Create a private bench group

In the Frappe Cloud dashboard: **Benches → New**. Pick the Frappe/ERPNext version (v15 or v16, both supported) and the region. Region matters for latency and for data residency; pick it deliberately.

### 3. Add the app

Bench dashboard → **Apps** → **Add App** → **Add from GitHub** → connect GitHub → select org, repo and branch → **Validate App** → **Add App**.

Validation reads `pyproject.toml`, so the Python dependencies (`openai`, `anthropic`, `tavily-python`, `httpx`) are resolved and installed at image build time. You do not pip install anything manually.

### 4. Deploy and install

**Show updates → Deploy.** This builds a new bench image, typically 5 to 15 minutes. Then on the site: **Apps → Install app → research_agent.**

### 5. Migrate

Frappe Cloud runs `bench migrate` as part of the site update. If DocTypes look missing afterwards, run it manually from the site's **Console** or over SSH:

```bash
bench --site erp.acme.com migrate
```

### Updating later

Push to GitHub → bench group → **Deploy** → then update the site. The app's `after_migrate` hook re-syncs the Agent Tool registry, so new tools appear without any manual step.

### What you get for free

Background workers, Redis, socketio (the live trace rail needs it), scheduler, SSL, backups. This is the least-work path and what I would recommend for most single-client deployments.

---

## Path B — AWS or any self-hosted bench

Same app, more of the plumbing is yours.

### Install

```bash
cd ~/frappe-bench
bench get-app https://github.com/YOURORG/research_agent.git
bench --site erp.acme.com install-app research_agent
bench --site erp.acme.com migrate
bench build --app research_agent
bench restart
```

### Four things that must be right

**1. The `long` queue must have a worker.** Research runs are enqueued with `queue="long"` and a 30 minute timeout. If no long worker is running, sessions sit at Queued forever and nothing tells you why.

```bash
bench setup supervisor && sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl status | grep worker
```

You should see `long`, `default` and `short` workers. On a busy site, raise the count in `common_site_config.json`:

```json
{ "workers": { "long": { "background_workers": 2 } } }
```

**2. Websockets must reach the browser.** The trace rail streams over socketio. If you are behind an ALB, CloudFront or a hand-written nginx config, the `/socket.io` path needs websocket upgrade headers proxied. Symptom when this is wrong: the answer appears at the end but the rail stays empty the whole time, which looks like the app is broken when it is working fine.

```nginx
location /socket.io {
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_pass http://frappe-socketio-server;
}
```

On an AWS ALB, set the target group's stickiness on and confirm the listener rule allows websocket upgrade.

**3. Outbound HTTPS on 443.** If the bench sits in a private subnet, it needs a NAT gateway or VPC endpoints. No outbound means every LLM call times out after 120 seconds and every session fails with a confusing traceback.

**4. Python 3.10+.** The app uses `X | None` type syntax throughout.

### Sizing

The agent is IO-bound, not CPU-bound. It waits on API calls. A t3.medium handles a small team fine; the constraint is the number of concurrent long workers, not raw compute. Sessions store full tool outputs, so budget for disk growth and check that the 90-day purge job is actually running (`Scheduled Job Type` list, search `purge_old_sessions`).

---

## Path C — Docker

Add the app to your `apps.json` before building the image:

```json
[
  { "url": "https://github.com/frappe/erpnext", "branch": "version-15" },
  { "url": "https://github.com/YOURORG/research_agent", "branch": "main" }
]
```

```bash
export APPS_JSON_BASE64=$(base64 -w 0 apps.json)
docker build \
  --build-arg=FRAPPE_PATH=https://github.com/frappe/frappe \
  --build-arg=FRAPPE_BRANCH=version-15 \
  --build-arg=APPS_JSON_BASE64=$APPS_JSON_BASE64 \
  --tag=yourorg/erpnext-agent:v1 \
  --file=images/custom/Containerfile .
```

Confirm the `queue-long` container is in your compose file. It is in the standard `frappe_docker` overrides but people trim it out.

---

## Setup, identical on every platform

Once installed, the app is off. It will not call an external API until someone deliberately turns it on.

**1. Check compatibility.** Open **Research Agent Settings** and press **Check Compatibility**. Confirm it detects your Frappe and ERPNext versions and sees the Manufacturing module.

**2. Add keys and test each one.** OpenAI or Anthropic, plus Tavily if you want web research. Each has a Test button that makes one real call. Keys are stored in Frappe's Password field type, encrypted with the site's `encryption_key`. They are per-site: two sites on one bench cannot see each other's keys.

**3. Set the model slots.** Planner `o4-mini`, worker `gpt-4.1-mini`, reflector `gpt-4.1` is a sensible starting point. Putting your strongest model on the worker slot is the usual way to burn budget for nothing.

**4. Set the DocType allowlist.** Leaving it empty means the agent can read anything the *user* can read. For a first rollout, name them: Sales Invoice, Sales Order, Payment Entry, Stock Entry, Work Order, BOM, Item, Bin, Customer, Company, Fiscal Year.

**5. Set the daily run limit.** 20 per user is the default. This is your cost ceiling.

**6. Tick Enabled.** Read-only mode is now live.

**7. Run the eval suite before anyone sees it.**

```bash
bench --site erp.acme.com execute research_agent.evals.harness.run_suite --kwargs "{'suite': 'daily'}"
```

Then run the permission case as a genuinely restricted user, not as Administrator:

```bash
bench --site erp.acme.com execute research_agent.evals.harness.run_suite \
  --kwargs "{'suite': 'safety', 'user': 'sales.exec@acme.com'}"
```

**8. Only then consider the write path.** Turn on Allow Write Actions, name two or three writable DocTypes, set an approver role, leave self-approval and auto-approve off. Watch a dozen requests come through before you loosen anything.

---

## Rolling out to multiple client sites

For an implementation practice putting this on several client ERPs, the shape is:

| | |
|---|---|
| **One bench group per client** if they are on Frappe Cloud with their own account, or one shared private bench if you host them |
| **Keys are per site.** Each client's Research Agent Settings holds their own keys. There is no shared pool and no cross-site visibility |
| **Billing** goes to whoever owns the OpenAI account. If you want per-client cost attribution, give each site its own API key rather than sharing one |
| **Updates** are a single deploy per bench group. Sites on the same bench update together |
| **Allowlists differ per client.** A manufacturing client needs Work Order and BOM; a services client does not. Do not ship one allowlist to everyone |

The tokens used per session are recorded on each Research Session (`input_tokens`, `output_tokens`, `duration_seconds`), so a simple report grouped by month gives you a per-client usage bill without any external metering.

---

## Regulated deployments

If a client cannot send ERP rows to a US-hosted API — GCC clients under PDPPL, anyone with a data-residency clause, some public sector — there are three options, in order of how much work they are:

**1. Point at a regional endpoint.** Azure OpenAI in UAE North or Qatar Central, or Amazon Bedrock in a regional AWS account. `agent/llm.py` is a provider router; adding Azure or Bedrock is a subclass of `BaseProvider` plus one entry in `PROVIDERS` and one option on the settings Select. It is the smallest change in this list and the one I would do first.

**2. Turn off web research and narrow the allowlist hard.** Reduces what leaves, does not eliminate it.

**3. Self-host an open model.** vLLM or Ollama behind an OpenAI-compatible endpoint. The `OpenAIProvider` class already speaks that protocol; you only need a base URL field. Quality on tool calling drops noticeably below the frontier models, so run the eval suite before promising anything.

Whatever you choose, write it into the client's DPA explicitly. "The AI reads our ERP" and "invoice rows are transmitted to a third-party API in the United States" are the same sentence to an engineer and very different sentences to a compliance officer.

---

## Remote access, the other direction

Once the app is installed, an MCP client can query the ERP from outside. This is the only topology where something genuinely "connects to" the ERPNext account.

1. Settings → tick **Expose ERPNext as an MCP Server**.
2. Create a normal ERPNext user with exactly the permissions you want exposed, and generate API keys for it (**User → Settings → API Access → Generate Keys**).
3. Point the client at the endpoint.

```json
{
  "mcpServers": {
    "erpnext": {
      "url": "https://erp.acme.com/api/method/research_agent.agent.mcp.server.handle",
      "headers": { "Authorization": "token API_KEY:API_SECRET" }
    }
  }
}
```

The connecting client is that ERPNext user. Every permission check in `erpnext_data.py` applies unchanged. Revoking access is deleting the API key or disabling the user. There is no separate token store to audit.

Only the data and analysis tools are exposed this way. Visualisation tools are pointless over MCP because the client draws its own UI, and write tools are excluded entirely.

---

## When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| Sessions stuck at Queued | No `long` queue worker | `bench setup supervisor`, check `supervisorctl status` |
| Answer appears but the trace rail stayed empty | Websockets not proxied | Fix the `/socket.io` location block or ALB listener |
| "No API key saved for OpenAI" after saving one | Site `encryption_key` changed, usually after a restore | Re-enter the keys on the restored site |
| Every session fails after ~120s | No outbound HTTPS | NAT gateway or VPC endpoint |
| DocTypes missing after install | Migrate did not run | `bench --site X migrate` |
| Tools missing from the list | Registry not synced | `bench --site X migrate` runs `sync_builtin_tools` |
| Agent says it cannot see data that exists | Working as designed | Check the user's roles and the DocType allowlist, in that order |
| MCP client gets 403 | Server toggle off, or bad API key format | It is `token KEY:SECRET`, not `Bearer` |

---

## Publishing to the Frappe marketplace

Once it is running on a real site and the eval suite passes:

1. Public GitHub repo with a licence, README and a tagged release.
2. Frappe Cloud dashboard → **Marketplace** → **Add App**, select the repo.
3. Add screenshots, a description and a support email. The walkthrough screens in `docs/walkthrough.html` are a reasonable starting point for the listing images.
4. Frappe reviews it. Expect questions about what leaves the network, since it calls external APIs. Answer them from the first section of this document.
5. Choose free or paid. Paid apps bill through Frappe Cloud subscriptions.

A listed app still needs a private bench to install, so marketplace listing widens discovery rather than lowering the setup bar.
