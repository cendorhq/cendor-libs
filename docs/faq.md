# FAQ

### Is this a web service / does it need a server or account?
No. These are **plain Python libraries** that run in your process — no server, no hosted account, no
network by default. "API" in the docs means the *Python interface* you import and call.

### Does it work offline / without API keys?
Yes for everything that doesn't call a model: token counting, pricing, context assembly,
compression, the audit hash chain, and spend reports all run offline. Only actual provider calls
need keys. Token counts are local; prices ship in a bundled snapshot.

### Which providers are supported?
`instrument()` supports **OpenAI, Anthropic, AWS Bedrock, Google Gemini, and Ollama** directly, plus
an OpenTelemetry **ingestion** path (`core.otel.ingest`) for managed runtimes (Foundry Agent
Service, OpenAI Assistants) where you don't own the loop. For OpenAI it wraps **both** the Chat
Completions API (`chat.completions.create`) **and** the Responses API (`responses.create`, primary
for new apps + the Agents SDK); for Gemini it detects **both** the current `google-genai` SDK
(`client.models.generate_content`, sync and async) and the legacy `google-generativeai`
`GenerativeModel.generate_content`. See [Providers & Integration](providers.md).

### Does it support streaming responses?
Yes. `instrument()` handles `stream=True` (sync and async): the chunk iterator passes through to
your code unchanged, and the normalized `LLMCall` is emitted **once the stream completes** — so
usage, cost, budgets, recording, and auditing all still work on streamed calls. (Usage is read from
the provider's stream where present, else an offline estimate flagged as such.)

### How is the model identified? What about models released in the future?
Three layers, two of them future-proof:
- **Capturing the call** is by client *shape* (`chat.completions.create`, `responses.create`,
  `messages.create`, `models.generate_content`, …), not model name — so a new model works the day
  it ships.
- **Token family** is a substring heuristic (`gpt*`/`claude`/`gemini`/…); a brand-new naming scheme
  falls back to a generic estimator (graceful, overridable via `tokens.register`).
- **Pricing** is a data table; a new model just shows `cost = None` until its rate is added via
  `prices.refresh(url=…)` (no library release needed).

### How accurate is token counting?
Three tiers, picked automatically — call `tokens.method(model)` to see which is active. With the
`[tiktoken]` extra installed, **OpenAI is exact** and **Claude/Gemini use tiktoken's `o200k` BPE as
a close estimate**. With no tokenizer installed, a character/subword heuristic is the fallback —
rough by nature (modern tokenizers run ~3–6 chars/token by content), so install `[tiktoken]` for
accuracy or `tokens.register(family, fn)` to plug in a precise counter. `Money` is always exact
(`Decimal`, never float).

### Can I get live / up-to-date prices?
Yes. A **dated snapshot ships bundled** so pricing works fully offline, and
`prices.refresh(source="litellm" | "openrouter" | "azure")` pulls **live** rates from unauthenticated
JSON sources (no API key, no extra dependencies). `prices.age_days()` and `prices.is_stale()` surface
how old your table is so you can decide when to refresh. Note that the model labs themselves
(OpenAI / Anthropic) expose **no pricing API** — only gateways, aggregators, and cloud catalogs
(LiteLLM / OpenRouter / Azure) publish machine-readable rates, which is why those are the sources.
See [Providers & Integration](providers.md).

### Is the cost an estimate or the real bill?
Both are surfaced, labelled honestly. When the provider or gateway reports an actual cost on the
response (e.g. OpenRouter's `usage.cost`), `instrument()` prefers it and labels it `cost_reported`;
otherwise it prices the call from the snapshot and labels it `cost_estimated`. Either way the **token
usage is the provider's real billed count**, and all money math is exact `Decimal` — never float.

### Can I use it in a server? Is it thread-safe?
Yes, within a process. Concretely, these shared structures are lock-guarded: `core`'s event bus and
interceptor registry, its lazy price-table load + `refresh()` swap and the `instrument()` install;
`tokenguard`'s spend buffer + FIFO eviction and its `SQLiteSink` (opened `check_same_thread=False`);
and `acttrace`'s hash-chain append (head + entries + durable file write). State is in-process and
module-global — ideal for a single worker. For a multi-*process* deployment, externalize durable
spend through a `tokenguard` sink (`SQLiteSink` / `OTelSink`) rather than the in-memory aggregate.

**One caveat — budgets and tags are `ContextVar`-based.** `budget(...)` and `track(...)` live in
`contextvars`, so an `asyncio` task **inherits** the active budget/tags (context is copied at task
creation), but a plain `threading.Thread` you start yourself does **not** — a spawned thread escapes
its parent's budget and loses its tags. To carry them into a thread, copy the context:

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

### Can I install just one library?
Yes. Each tool works standalone and pulls `cendor-core` transitively. Use `pip install
cendor` only if you want the whole stack.

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
