# FAQ

### Is this a web service / does it need a server or account?
No. These are **plain libraries** (Python and TypeScript) that run in your process — no server, no
hosted account, no network by default. "API" in the docs means the *interface* you import and call,
not a web endpoint.

### Does it work offline / without API keys?
Yes for everything that doesn't call a model: token counting, pricing, context assembly, compression,
the audit hash chain, and spend reports all run offline. Only actual provider calls need keys. Token
counts are local; prices ship in a bundled snapshot.

### Which providers are supported?
`instrument()` supports **OpenAI, Anthropic, Hugging Face, AWS Bedrock, Google Gemini, and Ollama**
directly, plus an OpenTelemetry **ingestion** path (`core.otel.ingest`) for managed runtimes (Foundry
Agent Service, OpenAI Assistants) where you don't own the loop. OpenAI wraps both Chat Completions and the Responses
API; Gemini detects both the current `google-genai` SDK and the legacy `google-generativeai`. See
[Providers & Integration](providers.md).

### Does it support streaming responses?
Yes — `stream=True`, sync and async. The chunk iterator passes through to your code unchanged, and the
normalized `LLMCall` is emitted once the stream completes, so usage, cost, budgets, recording, and
auditing all work on streamed calls. See [Providers → Streaming](providers.md#streaming) for how real
(vs estimated) the streamed usage is per provider.

### How is the model identified? What about models released in the future?
Three layers, two of them future-proof:

- **Capturing the call** is by client *shape* (`chat.completions.create`, `messages.create`, …), not
  model name — so a new model works the day it ships.
- **Token family** is a substring heuristic (`gpt*`/`claude`/`gemini`/…); a brand-new naming scheme
  falls back to a generic estimator (overridable via `tokens.register`).
- **Pricing** is a data table; a new model shows `cost = None` until its rate is added via
  `prices.refresh(...)` — no library release needed.

### How accurate is token counting?
Three tiers, picked automatically — call `tokens.method(model)` to see which is active. By default
**OpenAI is exact** and **Claude/Gemini use tiktoken's `o200k` BPE as a close estimate**, because
`tiktoken` is a required dependency of `cendor-core` — exact counting is not an opt-in. A
character/subword heuristic remains only as a defensive fallback if `tiktoken` ever fails to import
(a broken install); `tokens.register(family, fn)` plugs in a precise counter for any family. `Money`
is always exact (`Decimal`, never float). Details in [core → Token counting](core.md#token-counting-three-tiers).

### Can I get live / up-to-date prices?
Yes, with one honest caveat: **there is no real-time LLM pricing anywhere.** Every source on earth,
first-party included, is a catalog updated on change — not a ticker. "Live" here means *fetch the
current list price on demand*.

A **dated snapshot ships bundled** so pricing works offline, and `prices.refresh()` fetches the
[cendor-prices feed](https://github.com/cendorhq/cendor-prices) — a dated table with **per-row
provenance**, reconciled daily behind validation gates from the cloud catalogs and the MIT
aggregators. Or go straight to one source:
`prices.refresh(source="azure" | "aws" | "modelsdev" | "litellm" | "openrouter" | "vercel")`, each an
unauthenticated JSON GET (no key, no extra deps). `azure` and `aws` are the providers' **own** billing
catalogs and take a `region=`.

`prices.explain(model)` tells you which source a specific rate came from and its as-of date;
`age_days()` / `is_stale()` cover the table as a whole, and `tokenguard` warns once per process when a
USD budget estimates from a table older than 45 days. The model labs themselves (OpenAI / Anthropic)
expose **no pricing API** — only the clouds, gateways and aggregators do, which is why those are the
sources. See [Providers → Live pricing](providers.md#live-pricing).

### The price you have for my model is wrong. Can I override it?
Yes, and your override **wins over everything except the provider's own reported cost** — including
after a `refresh()`. Use `prices.register_model_price(id, input=…, output=…)` when you have a rate
card, `prices.register_deployment(name, like="gpt-4o")` for an Azure/Foundry deployment name, or
`prices.register(id, rates)` for exact per-token values. `prices.explain(id).registered` confirms
yours is the one in effect. If the *feed* is wrong rather than merely unusual for you, every number
and its source are in git — open an issue on `cendorhq/cendor-prices`.

### Is the cost an estimate or the real bill?
Both are surfaced, labelled honestly. When the provider or gateway reports an actual cost (e.g.
OpenRouter's `usage.cost`), `instrument()` prefers it and labels it `cost_reported`; otherwise it
prices from the snapshot and labels it `cost_estimated`. Either way the **token usage is the
provider's real billed count**, and all money math is exact `Decimal`.

### Can it block or redact unsafe input / output (guardrails)?
Yes — [`cendor-guardrails`](guardrails.md) is the **Gate** in the stack. Define a deterministic
check (`keyword_deny`, `regex_rule`, `url_allowlist` / `url_deny`, `length_bounds`, `json_schema`, or
`custom`) and attach it to one of four stages — input, tool call, tool output, output. `block` fails
closed (nothing spends), `redact` scrubs the payload and continues, `flag` records and continues.
Every decision emits on the bus, so `acttrace` chains it as tamper-evident evidence. The checks are
regex/arithmetic — microseconds, offline, $0 — so they **do not** stop a novel jailbreak they were
never told about; pair them with a bring-your-own model judge (`rules.llm_judge`) for open-ended
risk, and use `acttrace`'s `guard(Policy…)` for PII/secret detection (one detection engine, not two).

### Can I use it in a server? Is it thread-safe?
Yes, within a process. These shared structures are lock-guarded: `core`'s event bus and interceptor
registry, its price-table load/`refresh()` swap and the `instrument()` install; `tokenguard`'s spend
buffer + FIFO eviction and its `SQLiteSink`; and `acttrace`'s hash-chain append. State is in-process
and module-global — ideal for a single worker. For a multi-*process* deployment, externalize durable
spend through a `tokenguard` sink rather than the in-memory aggregate.

**One caveat — budgets and tags are `ContextVar`-based.** An `asyncio` task inherits the active
budget/tags (context is copied at task creation), but a plain `threading.Thread` you start does not.
To carry them into a thread, copy the context:

<!-- tabs: lang -->
<!-- tab: Python -->
```python
import contextvars, threading
ctx = contextvars.copy_context()          # captures the active budget + tags
threading.Thread(target=lambda: ctx.run(do_work)).start()   # do_work runs inside them
```
<!-- tab: TypeScript -->
> **Node is single-threaded**, so there's no cross-thread copy to worry about — the active budget
> and tags follow the async call scope via `AsyncLocalStorage`. Just `await` your work inside the
> `withBudget(...)` / `track(...)` callback.
<!-- /tabs -->

### Does it send my data or prompts anywhere?
No. There's no telemetry and no implicit network. OpenTelemetry export is opt-in. The only outbound
call in the whole stack is `prices.refresh()`, which you invoke explicitly to fetch a static price
snapshot (and it falls back to the bundled one offline).

### Is it tied to LangChain / an agent framework?
No — it's framework-agnostic. It wraps *around and inside* whatever loop you already have.

### Does Cendor work with LangChain / LangGraph?
Yes — via the framework's **callback system**, which is the SDK-aligned integration point. (An
instrumented inner client now covers most of it: LangChain calls through `with_raw_response`, which
core captures and prices from 1.14.1, and its structured-output branch from 1.14.2 — but its
*streaming* branch still bypasses `instrument()`, so the handler is the complete answer.) Install
`cendor-core[langchain]` and attach `CendorCallbackHandler`:

<!-- tabs: lang -->
<!-- tab: Python -->
```python
from cendor.core.langchain import CendorCallbackHandler
llm = ChatOpenAI(model="gpt-4o", callbacks=[CendorCallbackHandler()])
# LangGraph: agent.invoke(..., config={"callbacks": [CendorCallbackHandler()]})
```
<!-- tab: TypeScript -->
<!-- ts-check: skip -->
```ts
import { CendorCallbackHandler } from '@cendor/core/langchain';
const llm = new ChatOpenAI({ model: 'gpt-4o', callbacks: [new CendorCallbackHandler()] });
// LangGraph: agent.invoke(..., { callbacks: [new CendorCallbackHandler()] })
```
<!-- /tabs -->

It records usage + **reasoning** + cost + tool calls, and stamps a root-run `trace_id` so every
call of one `agent.invoke` is correlated. It is **recording-only** — pre-flight enforcement
(`tokenguard` `block`, `acttrace` `guard()`) needs the direct provider SDK + `instrument()`, since
the callback path never touches the client. See
[providers.md → Frameworks](providers.md#frameworks-langchain--langgraph).

### Does it work for multi-agent / multi-process systems?
Multi-agent within a process: yes — the LangChain callback path correlates each `agent.invoke`
under its own root-run `trace_id`, and for direct-SDK agents `core.trace("run-id")` sets an ambient
`trace_id`. Multi-*process*: state is process-local by design (local-first, no server), so
correlate by `trace_id` and aggregate durably via a `tokenguard` sink into your own store. cendor
provides a correlation *hook*, not a distributed orchestrator (see [architecture.md](architecture.md)).

### Will a long-running agent grow memory without bound?
No, if you bound the in-memory buffers. `tokenguard` FIFO-caps its spend buffer
(`configure(max_records=…)`, default 100k) and `acttrace` bounds its in-memory entry ring with
`AuditLog(path="audit.jsonl", max_entries=N)` — the **file stays the complete, verifiable chain**
while memory holds only the recent window (`evicted_from_memory` counts what left). For durable
history without per-call latency, wrap a sink in `tokenguard.sinks.QueueSink` (background-thread
I/O). See [acttrace → Long-running logs](acttrace.md#long-running-logs-max_entries) and
[tokenguard → QueueSink](tokenguard.md#queuesink--low-latency-durable-logging).

### Can I install just one library?
Yes. Each tool works standalone and pulls `cendor-core` transitively. Use `pip install cendor-libs`
(or its `cendor` alias) only if you want the whole stack.

### Does Cendor auto-upgrade, or tell me when a new version is out?
**No, and deliberately.** No Cendor library checks for updates, phones home, or opens a socket you did
not ask for. Your package manager owns your versions.

That is a product decision, not a missing feature. An upgrade changes what your budget refuses, what
your guardrail blocks, and what your audit chain records — silently changing that under a running
system would be the opposite of useful. And evidence you cannot tie to a known version is worth less
as evidence.

What we do instead:

- **SemVer + a changelog per package**, so a version number tells you what you are getting.
- **A machine-readable feed** at [`cendor.ai/releases.json`](https://cendor.ai/releases.json) (the
  human page is [/releases](https://cendor.ai/releases)). Fields are only ever added.
- **`doctor`**, which tells you what is behind when you ask —
  `uvx cendor-init doctor` offline, or `--online` against the live feed.
- **Deprecations warn in-band for at least two minors** before anything is removed, and removals only
  happen in a major. You will see it in your editor and your logs before it can break you.

For teams, the normal tools are the right answer: `pip list --outdated`, `uv lock --upgrade`,
`npm outdated`, or a grouped Renovate/Dependabot rule — there is a copy-paste config in
[Assistant + tooling setup](assistant-init.md#keeping-cendor-up-to-date).

> **Watch the lockfile.** A wide range (`cendor-core>=1.0,<2.0`) looks healthy while a `uv.lock` or
> `package-lock.json` beside it pins something years old, and the build stays green the whole time.
> `doctor` names the lock when the lock is the constraint.

### Does it secure my secrets?
`cassette` and `acttrace` redact emails, `sk-` keys (including the hyphenated `sk-ant-`/`sk-proj-`
forms), AWS and Google API keys, JWTs, and bearer tokens by default before writing (cassettes and
audit logs get committed/exported). Redaction is a best-effort regex safety net, not a guarantee —
keep real secrets out of prompts and inputs.

### Which versions are supported, and for how long?
**Fixes land on the latest minor of the current major.** If you are on `1.4.x` and the current release
is `1.7.x`, the fix goes into a `1.7.x` patch — upgrading within a major is designed to be
uneventful, because a caret or `>=1,<2` range already spans it.

**When a new major ships, the previous one gets security backports for six months.** Only security
fixes, no features and no behaviour changes, and each one is a patch on that older major's latest
minor. After six months the previous major is end-of-life: it keeps working and stays installable —
we do not unpublish releases — but it stops receiving fixes.

Two things worth saying plainly, because they are the honest shape of a project this size:

- **This is a support *window*, not a response-time commitment.** There is no SLA, no paid tier, and
  no support contract to buy. It tells you which branch a fix will appear on, not how fast.
- **Majors are not coupled across languages.** `cendor-*` on PyPI and `@cendor/*` on npm move majors
  independently, so each family's window is counted from its own major release. The
  [parity matrix](languages.md) is the contract, not matching version numbers.

Within a language, all the libraries share one major, so "the current major" is a single number you
can check on [/releases](https://cendor.ai/releases) — or ask `doctor`, which will tell you what is
behind.

### Does acttrace make me "EU AI Act compliant"?
No. It produces **evidence to support** compliance (record-keeping, human-oversight events,
tamper-evidence) — it is not legal advice or a guarantee, and the control mappings are starting
templates for your compliance team.

### How do I report a bug or request a feature?
Open an issue on the GitHub repo. Contributions are welcome via fork + pull request.
