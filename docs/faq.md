# FAQ

### Is this a web service / does it need a server or account?
No. These are **plain Python libraries** that run in your process — no server, no hosted account, no
network by default. "API" in the docs means the *Python interface* you import and call.

### Does it work offline / without API keys?
Yes for everything that doesn't call a model: token counting, pricing, context assembly, compression,
the audit hash chain, and spend reports all run offline. Only actual provider calls need keys. Token
counts are local; prices ship in a bundled snapshot.

### Which providers are supported?
`instrument()` supports **OpenAI, Anthropic, AWS Bedrock, Google Gemini, and Ollama** directly, plus
an OpenTelemetry **ingestion** path (`core.otel.ingest`) for managed runtimes (Foundry Agent Service,
OpenAI Assistants) where you don't own the loop. OpenAI wraps both Chat Completions and the Responses
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
Three tiers, picked automatically — call `tokens.method(model)` to see which is active. With
`[tiktoken]`, **OpenAI is exact** and **Claude/Gemini use tiktoken's `o200k` BPE as a close estimate**.
With no tokenizer installed, a character/subword heuristic is the fallback — rough by nature, so
install `[tiktoken]` for accuracy or `tokens.register(family, fn)` for a precise counter. `Money` is
always exact (`Decimal`, never float). Details in [core → Token counting](core.md#token-counting-three-tiers).

### Can I get live / up-to-date prices?
Yes. A **dated snapshot ships bundled** so pricing works offline, and
`prices.refresh(source="litellm" | "openrouter" | "azure")` pulls **live** rates from unauthenticated
JSON sources (no key, no extra deps). `prices.age_days()` / `is_stale()` tell you how old your table
is. The model labs themselves (OpenAI / Anthropic) expose **no pricing API** — only gateways,
aggregators, and cloud catalogs do, which is why those are the sources. See [Providers → Live pricing](providers.md#live-pricing).

### Is the cost an estimate or the real bill?
Both are surfaced, labelled honestly. When the provider or gateway reports an actual cost (e.g.
OpenRouter's `usage.cost`), `instrument()` prefers it and labels it `cost_reported`; otherwise it
prices from the snapshot and labels it `cost_estimated`. Either way the **token usage is the
provider's real billed count**, and all money math is exact `Decimal`.

### Can I use it in a server? Is it thread-safe?
Yes, within a process. These shared structures are lock-guarded: `core`'s event bus and interceptor
registry, its price-table load/`refresh()` swap and the `instrument()` install; `tokenguard`'s spend
buffer + FIFO eviction and its `SQLiteSink`; and `acttrace`'s hash-chain append. State is in-process
and module-global — ideal for a single worker. For a multi-*process* deployment, externalize durable
spend through a `tokenguard` sink rather than the in-memory aggregate.

**One caveat — budgets and tags are `ContextVar`-based.** An `asyncio` task inherits the active
budget/tags (context is copied at task creation), but a plain `threading.Thread` you start does not.
To carry them into a thread, copy the context:

```python
import contextvars, threading
ctx = contextvars.copy_context()          # captures the active budget + tags
threading.Thread(target=lambda: ctx.run(do_work)).start()   # do_work runs inside them
```

### Does it send my data or prompts anywhere?
No. There's no telemetry and no implicit network. OpenTelemetry export is opt-in. The only outbound
call in the whole stack is `prices.refresh()`, which you invoke explicitly to fetch a static price
snapshot (and it falls back to the bundled one offline).

### Is it tied to LangChain / an agent framework?
No — it's framework-agnostic. It wraps *around and inside* whatever loop you already have.

### Does Cendor work with LangChain / LangGraph?
Yes — via the framework's **callback system**, which is the SDK-aligned integration point (not
inner-client wrapping: LangChain calls the client through `with_raw_response`, so instrumenting it
loses usage). Install `cendor-core[langchain]` and attach `CendorCallbackHandler`:

<!-- tabs: lang -->
<!-- tab: Python -->
```python
from cendor.core.langchain import CendorCallbackHandler
llm = ChatOpenAI(model="gpt-4o", callbacks=[CendorCallbackHandler()])
# LangGraph: agent.invoke(..., config={"callbacks": [CendorCallbackHandler()]})
```
<!-- tab: TypeScript -->
> **Python only (for now).** The LangChain callback handler ships in Python; in TypeScript, wrap the
> provider client with `instrument()` directly. See the [parity matrix](/docs/languages).
<!-- /tabs -->

It records usage + **reasoning** + cost + tool calls, and stamps a root-run `trace_id` so every
call of one `agent.invoke` is correlated. It is **recording-only** — pre-flight enforcement
(`tokenguard` `block`, `acttrace` `guard()`) needs the direct provider SDK + `instrument()`, since
the callback path never touches the client. See
[providers.md → Frameworks](providers.md#frameworks-langchain--langgraph).

### Does it work for multi-agent / multi-process systems?
Multi-agent within a process: yes — the LangChain callback path correlates each `agent.invoke`
under its own root-run `trace_id`, and for direct-SDK agents `core.trace("run-id")` sets an ambient
`trace_id`. Multi-*process*: state is process-local by design (no server — CLAUDE.md rule 4), so
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

### Does it secure my secrets?
`cassette` and `acttrace` redact emails, `sk-` keys (including the hyphenated `sk-ant-`/`sk-proj-`
forms), AWS and Google API keys, JWTs, and bearer tokens by default before writing (cassettes and
audit logs get committed/exported). Redaction is a best-effort regex safety net, not a guarantee —
keep real secrets out of prompts and inputs.

### Does acttrace make me "EU AI Act compliant"?
No. It produces **evidence to support** compliance (record-keeping, human-oversight events,
tamper-evidence) — it is not legal advice or a guarantee, and the control mappings are starting
templates for your compliance team.

### How do I report a bug or request a feature?
Open an issue on the GitHub repo. Contributions are welcome via fork + pull request.
