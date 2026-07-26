# Languages & parity

Cendor ships in two languages: **Python** (`cendor.*`, the reference implementation, on PyPI) and
**TypeScript/JavaScript** (`@cendor/*`, on npm — ESM-only, Node LTS first, edge runtimes
supported). Both are implementations of the same versioned [format specs](https://github.com/cendorhq/cendor-libs/tree/main/docs/specs),
so the artifacts that matter — cassettes, audit chains, price tables, bus events — are
**byte-for-byte interoperable**, checked by committed conformance vectors in both CIs — each
language verifies artifacts written by the other (the TypeScript CI replays Python-written
fixtures; the Python CI verifies a JS-written audit chain).

<!-- tabs: lang -->
<!-- tab: Python -->

```bash
pip install cendor-libs        # the whole stack; `cendor` is an alias
```

```python
from cendor.core import instrument
client = instrument(OpenAI())
```

<!-- tab: TypeScript -->

```bash
npm i @cendor/libs             # the whole stack (umbrella)
```

```ts
import { instrument } from '@cendor/core';
const client = instrument(new OpenAI());
```

<!-- /tabs -->

## One API, two spellings

The TypeScript API is *derived from* the Python public API, mechanically:

| Rule | Python | TypeScript |
|---|---|---|
| Names | `snake_case` (`max_turns`, `on_exceed`) | `camelCase` (`maxTurns`, `onExceed`) |
| Scopes | context managers (`with budget(...):`) | async callbacks (`withBudget(cfg, fn)`) / decorators (`budget(cfg)(fn)`) |
| Money | `Decimal` — never a float | `decimal.js` — never a float; value-equal across languages |
| Errors | `BudgetExceeded`, `PolicyViolation`, … | identical names |
| Defaults | the same | the same |
| Tool schemas (SDK) | derived from type hints + docstring | declared with [zod](https://zod.dev) |

## Cross-language guarantees

These are tested by committed conformance vectors, not promised:

- A **cassette** recorded in Python **replays in TypeScript** (and vice-versa) — same request
  hashes, same wire format.
- An **audit chain** written in TypeScript **`verify()`s in Python** — identical canonical bytes
  and HMAC inputs.
- **Prices and token counts match exactly** — same bundled snapshot, `tiktoken` ↔ `js-tiktoken`
  parity, decimal-exact math.
- **Bus events** (`LLMCall` / `ToolCall` / `Usage` / `Money`) share one schema across languages.

## Parity matrix — libraries

Legend: ✅ ported · 🚧 partial/scoped · **Py-only** deliberately not ported.

| Capability | Python | TypeScript | Notes |
|---|---|---|---|
| `Money` (decimal, never float) | ✅ | ✅ | `Decimal` ↔ `decimal.js`; value-equal across langs |
| `Usage` / `LLMCall` / `ToolCall` | ✅ | ✅ | snake_case ↔ camelCase fields; type names identical |
| Event bus | ✅ | ✅ | subscribe/emit/unsubscribe; error isolation |
| Price table + `estimate()` | ✅ | ✅ | same bundled snapshot; `refresh()` async in TS |
| Token counting | ✅ | ✅ | `tiktoken` ↔ `js-tiktoken` — exact counts match |
| `instrument()` providers | ✅ 6 (OpenAI, Anthropic, HuggingFace, google-genai, Bedrock, Ollama) | ✅ 6 (OpenAI, Anthropic, HuggingFace, google-genai, Bedrock, Ollama) | Bedrock auto-detects a boto-shaped `converse()` **and `converse_stream`** (an always-stream target — TS since `@cendor/core` 0.12.2, Python since core 1.10); aws-sdk-v3 `send(ConverseCommand)` rides the SDK provider |
| `instrument()` streaming / interceptors | ✅ | ✅ | |
| core `otel` spans / `ingest()` | ✅ | ✅ | `span()` + `ingest()`; `@opentelemetry/api` optional peer — span is a no-op without it |
| **Observability export** ([Observability](observability.md)) | ✅ | ✅ | `OTelSink` (spend metrics, dimensioned by `track` tags), SDK `live_spans`/`span_tree` (accept `conversation_id`/`conversationId` → `gen_ai.conversation.id`; a `label`/`label` → `cendor.run.label`; every child carries `gen_ai.agent.name` + a 1-based `cendor.step`; a streamed `chat` span carries `cendor.ttft_ms` + `cendor.usage_estimated="true"` when the count was estimated offline; the live root reaches parity with the post-hoc tree incl. `cendor.run.agents`; since SDK ≥ 1.15 / 0.20 the TS span tree matches Python's finer set — `gen_ai.system`, `gen_ai.latency_ms`, `finish_reason`, streamed flag, `error`, tool `arg_names`; live child spans backdated by latency; a 3-level root → per-agent → call tree), and `AuditLog(mirror=OTelMirror())` — governance/audit → any OTel backend; export to Azure Monitor / CloudWatch / Datadog / OTLP with zero Cendor-specific exporter |
| **Automatic telemetry** (`CENDOR_TELEMETRY`; core ≥ 1.13 / 0.15, sdk ≥ 1.19 / 0.22) | ✅ | ✅ | With OpenTelemetry installed **and a provider configured by your app**, emission needs no code: the call-span emitter arms at the first `instrument()`/`ingest()`, tokenguard writes spend through an internal additive tap (the `use_sink` slot stays yours), `AuditLog(...)` auto-attaches its `OTelMirror` (`mirror=False`/`mirror: false` opts out), `run()` opens the run scope itself, and budget/guardrail decisions ride `governance.*` spans (`cendor.gov.*`, no `audit.*` vocabulary, no `reason` — rule 6; suppressed while a mirror is on the wire). `CENDOR_TELEMETRY=off` kills all of it; `CENDOR_DEBUG_TELEMETRY=1` explains what was detected. Explicit attachments still win. **Parity note:** the predicate is `ProxyTracerProvider` (Py) vs the proxy's `getDelegate()` (TS); the live-spans latch is a `ContextVar` in Python (its `live_spans` is a context manager, so it is always scoped) but has two mechanisms in TS, where a `liveSpans()` handle is closed by hand: **process-wide while a manual handle is open**, and `AsyncLocalStorage.run()`-scoped for the SDK's automatic run scope. ⚠️ `@cendor/core` 0.13.0–0.15.0 used `enterWith`, which only scopes as intended on **node ≥ 24** — on node 20 / 22 a closed scope left the emitter suppressed process-wide; fixed in core **0.15.1** + `@cendor/sdk` **0.23.1** (verified on node 20.20 / 22.23 / 24.18) ⚠️ `cendor-sdk` < **1.19.1** / `@cendor/sdk` < **0.23.2** mis-attributed **concurrent** runs: a scope learned its run family from the first bus event it saw (a process-wide fanout), so two overlapping runs rendered one run's call twice — once under each root — dropped the other's, and stamped both roots with one `cendor.run.id`; a run-less libs call was adopted as the run's step 1. Fixed: one owning scope per run family, and the automatic scope learns only from its own context. |
| **Opt-in content capture** (core ≥ 1.7 / 0.7) | ✅ | ✅ | `otel.capture_content()`/`captureContent()` (or `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`) — OFF by default. On, `span_tree`/`live_spans` stamp `gen_ai.input.messages` / `output.messages` (incl. parsed **thinking** parts) / `system_instructions` + tool arg/result; masked (fail-closed) + byte-capped; **never** on `audit.*` (rule 6). Auto `gen_ai.conversation.id` from `run(session=…)`. Libs-only apps light up via `otel.use_span_emitter()`. Streamed calls stamp `cendor.ttft_ms` (now on the SDK `span_tree`/`live_spans` journeys too, not just the libs-only emitter) + `cendor.usage_estimated="true"` when the count was estimated offline |
| **squeeze compression event** (squeeze ≥ 1.1 / 0.3) | ✅ | ✅ | `CompressionEvent` on the bus after each `compress()` (metadata only — technique, tokens before/after, ratio, store kind, handle id; never text); acttrace (≥ 1.8 / 0.9) chains it as a `compression` entry + `audit.compression` mirror span |
| acttrace `budget_event` chaining + `mirror=` completeness | ✅ | ✅ | `tokenguard` emits `BudgetEvent` (blocked/downgraded/clamped) on the bus, now carrying `budget(name=…, description=…)`; acttrace chains it + optional `OTelMirror`. Since acttrace 1.7 / 0.8 the mirror carries the budget name (`cendor.audit.budget`) + numeric projected-vs-cap (money as strings), `llm_call` usage/latency/replayed, guardrail agent/tool + nested severity/policy_version/policy_hash, and context_assembly block counts. File stays the sole `verify()` evidence; `otel_trace_id`/`otel_span_id` correlation on entries. **acttrace ≥ 1.9 / 0.10** additionally stamps `run_id` (Cendor's ambient run id from the SDK `run()` scope) → `cendor.audit.run_id`, the fallback a trace-aware tool joins on when no OTel span was active (post-hoc `span_tree` / no context manager). **@cendor/sdk ≥ 0.17** makes TS `liveSpans` activate the run root (Python `live_spans` always did), so a TS SDK run inside `liveSpans` gets during-run governance→run correlation at parity with Python |
| Native governance counters | ✅ | ✅ | `cendor.tokenguard.budget.events` (tg ≥ 1.3 / 0.4) + `cendor.guardrails.decisions` (gr ≥ 1.6 / 0.7) — no-op without OpenTelemetry; chart budget-block / guardrail-decision **rates** (`*_total` in Prometheus) |
| LangChain `CendorCallbackHandler` | ✅ | ✅ | `@cendor/core/langchain`; recording-only in both; reads `usage_metadata`, correlates by root-run `traceId` |
| openai-agents `CendorAgentHooks` / `observeOpenAIAgents` | ✅ | ✅ | extra `[openai-agents]` / subpath `@cendor/core/openai-agents`; sources the framework's agent name per turn (set at start/handoff, cleared at end). The agent's calls ride the standard OpenAI client, so `instrument()` still captures tokens/cost/streaming. Never-overwrite. Py = a `RunHooks` you pass to `Runner.run(hooks=…)`; TS = `observeOpenAIAgents(runner\|agent)`. **Mechanism (both):** a process-wide holder read live at call construction (the Agents SDK isolates the model call's async context from the hooks, so a contextvar / `AsyncLocalStorage` never reaches it) — **honest limit:** exact for sequential runs + handoffs, but concurrent `Runner.run()` in one process may cross-attribute the name |
| Foundry agent correlation adapter | ✅ | ✅ | extra `[foundry]` / subpath `@cendor/core/foundry`; `observe_foundry_agents(client)` / `observeFoundryAgents(client)` wraps thread-run creation to stamp `agent` + `conversation_id`. **Attribution-only** — the model runs server-side, so **no per-step token/cost** (documented honest limit). Duck-typed on `.runs` (no SDK import needed to import the adapter) |
| `trace()` correlation | ✅ contextvars | ✅ AsyncLocalStorage | Full parity. |
| **`trace()` as a real parent span** | ✅ (core ≥ 1.14.0) | ✅ (`@cendor/core` ≥ 0.16.0) | Same shape both languages: a `cendor.trace <id>` span (scope `cendor.core`, `cendor.run.id` + `cendor.scope="trace"`) whose children are the calls, each with a 1-based `cendor.step` — **one scope, one trace**. **Behaviour change:** before those versions the scope only stamped an ambient id, so each call was its own root span. Opt out with `CENDOR_TRACE_SPAN=off`, or `span=False` / `{ span: false }`. Mechanism: Python `ContextVar` + `start_as_current_span`; TS `startActiveSpan` (i.e. `context.with`, an `AsyncLocalStorage.run()` — never `enterWith`), verified in docker on node 20.20 / 22.23 / 24.18. **TS-only fix riding along:** the ambient id now isolates itself with a real `AsyncLocalStorage` **by default** on Node — before 0.16.0 it fell back to a module variable unless a host called `installTraceContext`, so two *overlapping* scopes shared one variable. |
| **`gen_ai.agent.id`** | ✅ (core ≥ 1.14.0 / sdk ≥ 1.20.0) | ✅ (`@cendor/core` ≥ 0.16.0 / `@cendor/sdk` ≥ 0.24.0) | `Agent(id=…)` / `new Agent({ id })`; emitted on the live tree, `span_tree`, and the flat libs emitter. **Only when given** — absent, the attribute is omitted (never hashed, never a placeholder). No provider returns an agent id for a plain chat call. |
| **Provider-native agent identity (adapters)** | ✅ `cendor.core.agent_ids` (core ≥ 1.14.0) | ✅ `@cendor/core/agent-ids` (≥ 0.16.0) | `bedrock_agent_scope` / `bedrockAgentScope` (`agentId`[+alias] → `gen_ai.agent.id`, `sessionId` → `gen_ai.conversation.id`), `openai_assistant_scope` / `openaiAssistantScope`, and the generic `agent_scope` / `agentScope`. Foundry's adapter now maps its `agent_id` onto `gen_ai.agent.id` too. **Honest limit, both languages:** all of these are **attribution-only** — mapping identity does not make a server-side runtime's tokens or cost appear, because no model call passes through `instrument()` there. |
| **The actor on a governance row** | ✅ core ≥ 1.14.0 + acttrace ≥ 1.13.0 | ✅ `@cendor/core` ≥ 0.16.0 + `@cendor/acttrace` ≥ 0.14.0 | `ambient_attrs()` / `ambientAttrs()` lets a governance record name the acting agent (and its id) without acttrace/tokenguard importing the SDK. `governance.*` ops spans get `cendor.gov.agent`/`_id`; the `OTelMirror` stamps `cendor.audit.agent`/`_id` on **every** entry, including the types with no agent field (a budget block, a decision record, an `llm_call`). Measured before: 13 of 386 rows named their agent. |
| **Ambient metadata seam** (core ≥ 1.9 / 0.10) | ✅ | ✅ | `add_ambient_provider(fn)` / `remove_ambient_provider` (TS `addAmbientProvider`/`removeAmbientProvider`) — a `(event) → metadata \| None` provider stamps run-scoped metadata (`agent` / `conversation_id` / `decision_id` …) onto every `LLMCall`/`ToolCall` **at construction**, before interceptors: never-raise, never-overwrite, registration order, zero-provider fast path. Core stays generic (learns no SDK vocabulary); `metadata["agent"]` → `gen_ai.agent.name` on the span, so a libs-only app surfaces the agent name too |
| **tokenguard** budgets / track / report / sinks | ✅ | ✅ | SQLite / Queue / OTel sinks in both |
| **guardrails** rules / stages / install / scoped / adapters | ✅ | ✅ | deterministic gate at 4 stages (input / tool_call / tool_output / output); block / redact / flag → `guardrail_decision` on the bus; `apply` / `evaluate` (+ async), `install()` interceptor, `scoped()` per-request gating (contextvars / AsyncLocalStorage), per-guardrail `timeout` + `on_error`, `judge` helpers, detection-tier adapters (`classifier`, `language`, `openai_moderation`). `prompt_guard` (transformers) is **Python only** — in TS wire a classifier via `rules.classifier`. `@cendor/guardrails` core is pure/all-runtime (no hard `node:*`). Naming exception: the custom-guardrail factory is Py `guardrail(name, check)` ↔ TS `defineGuardrail(name, check)` |
| **guardrails** hosted rails | ✅ | ✅ | `bedrock_guardrail` (AWS ApplyGuardrail), `azure_content_safety` (Prompt Shields), `model_armor` (Google) — duck-typed clients (no cloud SDK imported), metered by the vendor; every verdict still emits a **local** `guardrail_decision` ("cloud check, local evidence") |
| **guardrails** config-as-data (`load_policy`) | ✅ | ✅ | declare deterministic rules in a versioned JSON/YAML file; the content hash + version are stamped into every decision's `metadata` (`policy_hash` / `policy_version`) so the audit chain proves which policy was active. YAML via the `[yaml]` extra (Py) / a BYO parser (TS) |
| **guardrails** groundedness / denied topics | ✅ | ✅ | `groundedness` / `denied_topics` over a **bring-your-own** `embed(text)` fn (cassette's BYO-scorer precedent) — cosine similarity, no bundled model, no accuracy claim |
| **guardrails** matching maturity (G1) | ✅ | ✅ | `keyword_deny(match="word", normalize=…)` — opt-in Unicode word boundaries + NFKC/zero-width/casefold folding (default substring, byte-for-byte back-compatible); `metadata["matched"]` records the term. JS uses `\p{L}\p{N}_` lookarounds (its `\b` is ASCII-only) |
| **guardrails** custom categories (G2) | ✅ | ✅ | `custom_category(name, examples, embed=…)` — semantic category-by-example (Azure "rapid custom categories" done local, `$0`); the paraphrase catch a deny-list misses. `embed` is BYO; no catch-rate claim |
| **guardrails** local embedder (G2) | ✅ `local_embedder` (model2vec, sync) | ✅ `localEmbedder` (transformers.js, async) | a zero-config offline `embed` behind an optional extra: **Py** `embeddings.local_embedder()` (the `[embeddings]` extra, **model2vec** static embeddings, numpy-only, **sync**); **TS** `embeddings.localEmbedder()` (the optional `@huggingface/transformers` peer, **async**). No maintained model2vec JS port exists, so the backends differ — and `embed` may be sync **or** async (an async embed gates via `applyAsync`/the SDK loop). No catch-rate claim |
| **guardrails** intent screening (G3) | ✅ | ✅ | `rules.intent(intents, embed=…\|classify=…, mode="deny"\|"allow")` — a first-class pre-LLM intent gate (deny topics you don't serve / off-topic gate); `judge.intent_prompt`/`intentPrompt` is the LLM-judge backend. No accuracy claim, no bundled taxonomy |
| **guardrails** presets + policy schema (G4) | ✅ | ✅ | `presets.PROMPT_INJECTION_EN` / `prompt_injection()` (curated starter list — inline code, **not detection**, no coverage claim) + `policy_schema()`/`policySchema()` + `load_policy(validate=True)` (stdlib structural check, no `jsonschema`). Py ships `policy.schema.json`; TS ships the schema inline (all-runtime) |
| **guardrails** Azure adapter breadth (G5) | ✅ | ✅ | `azure_content_safety(checks=("harm_categories",), harm_threshold=…, blocklist_names=…)` now also wraps Azure's `analyze_text` harm classifier (severity → `metadata["severity"]`) + blocklists, alongside Prompt Shields (default). Groundedness-as-a-service is a planned follow-up |
| **guardrails** red-team eval | ✅ | ✅ | `run_redteam` + `load_corpus` — trip rate + false-positive rate + per-category breakdown against a labeled corpus **you** supply (no vended data). Py reads a file path; TS takes text/array (no `node:fs`) |
| **guardrails** `spotlight` (A1) | ✅ | ✅ | deterministic, `$0`, offline `redact`-action **mitigation** (inspired by Azure Spotlighting): wraps untrusted content (`input` / `tool_output`) in a trust-lowering delimiter (optionally base-64). Never blocks; a mitigation, not a detector |
| **guardrails** annotation-parity metadata (A2) | ✅ | ✅ | reserved `GuardrailDecision.metadata` keys (`severity` / `detected` / `filtered` / `redacted` / `citation` / `license`) — no event-shape change, no acttrace edit; a check attaches them via `Verdict.metadata` and the adapters populate them from the vendor result |
| **guardrails** `task_adherence` (A3) | ✅ | ✅ | BYO-judge alignment check at the `tool_call` stage (does the proposed call match the user's intent?), via `judge.task_adherence`/`judge.taskAdherence` + `Context.instruction`. The `@cendor/guardrails` helper is ported and the **SDK-JS** auto-threading of the instruction shipped in `@cendor/sdk` 0.7.0 |
| **guardrails** SDK re-ask / stream window | ✅ | ✅ (TS since SDK 0.20) | `Agent(reask_on_output_trip=N)`/`reaskOnOutputTrip` (bounded re-ask on an output block, **non-streaming**) + `Agent(stream_check_window=N)`/`streamCheckWindow` (incremental `run.stream` output check) — now in **both** languages. Streaming re-ask is intentionally offered in neither (a streamed answer's deltas can't be unshown to re-ask) |
| **contextkit** assemble / evict / order | ✅ | ✅ | TS collapses sync+async into one `async assemble()` |
| **squeeze** compress / decompress | ✅ | ✅ | deterministic; handle ids match |
| **cassette** record / replay | ✅ | ✅ | cross-language replay, vector-verified |
| cassette `local_embedding_scorer` (bundled model2vec) | ✅ | **Py-only** | no JS static-embedding package exists; TS uses the BYO `embeddingScorer(embedFn)` / `openaiEmbeddingScorer` seam instead. The TS `localEmbeddingScorer` symbol **exists but throws** with that guidance (a deliberate Type-Teach stub, not a silent absence) |
| cassette storage | fs | fs + memory | pluggable adapters — an in-**memory** adapter ships in TS; **IndexedDB is implementable** via the `CassetteStorage` interface but is **not** a shipped adapter |
| **acttrace** chain / verify / sign | ✅ | ✅ | cross-language verify (HMAC + `_meta`) |
| acttrace detectors | ✅ regex **+ Presidio NER** (the `[ner]` extra + a `spacy download` model) | ✅ regex/pattern (20 detectors) **+ NER** | 🚧 NER via optional `compromise` (English-only, lighter than Presidio — not parity); `nerAvailable()` reports presence. Python's `[ner]` needs a spaCy model installed separately (see Honest limits) |

## Parity matrix — SDK

| Capability | Python | TypeScript |
|---|---|---|
| `Agent` / `tool` / `run` / `Result` | ✅ | ✅ (zod tool schemas) |
| Providers | ✅ ten paths | ✅ ten paths (OpenAI, Anthropic, HuggingFace, Azure chat + responses, Foundry Local, Ollama, Gemini, Bedrock) — HF/Ollama/Gemini/Bedrock usage capture rides `@cendor/core`'s provider detection |
| Sessions & memory | ✅ (+ SQLite store) | ✅ (better-sqlite3 + memory adapters) |
| Handoff / supervisor / pipelines | ✅ | ✅ | `run([entry, ...peers])` handoff teams + `sequential` / `parallel` / `parallel_async` pipes + `supervisor`, both languages. Per-run governance is honoured on every shape — `audit` / `max_turns` / `retry` / `on_step` / `guardrails` (a per-run override; decisions collected into `Result.guardrail_decisions`). `session` / `checkpoint` are **team-only** (`run([...])` / `supervisor`) — a pipe has no single conversation to persist; `guardrail_mode` is a single-agent-run option. Matched 1:1 (Py `supervisor` delegates to `run_agents` exactly as TS `supervisor`→`runAgents`; SDK ≥ 1.17 / 0.21) |
| Structured output | ✅ | ✅ — **native Anthropic** `output_config.format` json_schema on supported models (SDK ≥ 1.14 / 0.19), older models degrade to the JSON-instruction nudge; **Bedrock** forces a synthetic-tool `toolChoice` (SDK ≥ 1.15 / 0.20) **gated to tool-less agents** (can't coexist with real tools on Converse), else the nudge |
| Streaming | ✅ incremental (OpenAI, Ollama, **Anthropic** SDK ≥ 1.14 / 0.19) | ✅ incremental (OpenAI, Ollama, **Anthropic** SDK ≥ 0.19); single-agent + multi-agent |
| `ThinkingDelta` stream event (SDK ≥ 1.13 / 0.18) | ✅ | ✅ streamed reasoning, separate from `TextDelta`, for providers that stream it — Ollama `think`, OpenAI-compatible `reasoning_content`, and **Anthropic `thinking_delta`** (both langs, SDK ≥ 1.14 / 0.19) |
| Multimodal images (Ollama / Bedrock) | ✅ Ollama `images[]` + Bedrock Converse image blocks from data-URLs (SDK ≥ 1.14) | ✅ (SDK ≥ 0.19) — remote http(s) image URLs unsupported (no fetching), documented |
| Governance re-exports | ✅ | ✅ (the real `@cendor/*` objects) |
| `guard` identity + scope form | ✅ `sdk.guard is acttrace.guard`; `with guard(...):` | ✅ `Object.is`; `guard(opts, fn)` — dual-shape acttrace ≥ 1.5.0 / 0.6.0, SDK ≥ 1.7.0 / 0.10.0 |
| SDK `rules` = full library catalogue | ✅ all factories + the `pii`/`secrets`/`entropy` bridge | ✅ since 0.10.0 — spotlight, detection-tier adapters, and similarity checks included (`payloadText`/`NORMALIZATIONS` helpers stay library-only) |
| Embeddings capture + pre-flight governance | ✅ `embed()` rides `instrument()` (core ≥ 1.6.0) | ✅ (core ≥ 0.6.0) — a keyless USD budget blocks an embed before it fires |
| `EvalCase` cassette `normalizer` passthrough | ✅ | ✅ |
| `downgrades()` / `clamps()` re-export | ✅ | ✅ |
| Parity/identity CI (re-export drift fails the build) | ✅ `tests/test_lib_parity.py` | ✅ `test/lib-parity.test.ts` |
| Live progress / prompt caching / live OTel spans | ✅ | ✅ |
| MCP client (tools / prompts / resources) | ✅ | ✅ (`@modelcontextprotocol/sdk` optional peer) |
| Checkpoint / resume | ✅ | ✅ (atomic JSON; single + multi-agent; **streamed runs too** since SDK ≥ 1.15 / 0.20 — `run.stream`/`run.astream` take `checkpoint`, done-resume replays a lone `RunComplete`, an unfinished resume skips prepare and does not re-yield prior deltas. Py `run.astream` accepted `checkpoint=` but did not forward it until **1.17** — fixed, now at parity with `run.stream` and TS) |
| **Structural telemetry spans** (SDK ≥ 1.16 / 0.21) | ✅ | ✅ | opt-in `cendor.sdk` child spans for **RAG** (`rag.assemble`/`rag.compress`), **memory** (`memory.load`/`save`), **orchestration** handoffs, **checkpoints**, a first-class **tool** domain (source `local`\|`mcp`, outcome `ok`\|`error`\|`blocked`), and **MCP** server attribution (`mcp.connect`/`mcp.list_tools`) + a forward-compat `sdk_events` envelope — zero-core, both languages, content rules unchanged (labels/ids/counts, never bodies). Rendered by **Cendor Monitor 0.9**'s SDK-door structure pages (Orchestration · Tools · MCP · RAG · Memory · Checkpoints) |
| A2A server / client | ✅ | ✅ (JSON-RPC; `serve()` on node:http) |
| Foundry / Bot-Framework adapter | ✅ | ✅ |

## Runtime targets (TypeScript)

| Package | Node | Edge (Workers) | Browser |
|---|---|---|---|
| `@cendor/core` | ✅ | ✅ | 🚧 types/bus/prices/tokens are pure; `instrument` wraps fetch SDKs |
| `@cendor/contextkit`, `@cendor/squeeze` | ✅ | ✅ | ✅ pure compute |
| `@cendor/tokenguard` | ✅ | ✅ | ⚠️ advisory only — enforcement is server-side |
| `@cendor/cassette` | ✅ (fs) | ✅ (adapter) | ⚠️ memory/IndexedDB adapter |
| `@cendor/acttrace` | ✅ | ✅ | ❌ never — signing keys can't live in a client |
| `@cendor/sdk` | ✅ | ✅ (HTTP/SSE transports; MCP via `@modelcontextprotocol/sdk`, Node) | ❌ keys-in-browser anti-pattern |

> **Governance is only real where the user can't tamper with it.** Budgets-as-enforcement,
> audit-as-evidence, and redaction-as-guarantee are server-side by definition — in every
> language. Browser builds of the pure-compute libraries are UX aids (live token/cost preview,
> context assembly in the chat UI), and that's all they claim to be.

## Honest limits

- **Versions are independent across languages.** Python and TypeScript release on their own
  cadence; this page — not matching version numbers — is the parity contract.
- **A couple of surfaces remain Python-only** — cassette's bundled `local_embedding_scorer` (bring
  your own `embedFn` in TS). AWS Bedrock auto-detection matches a boto-shaped `converse()`;
  aws-sdk-v3's `send(ConverseCommand)` is captured via the SDK provider rather than `instrument()`.
  (The LangChain / LangGraph callback handler is now in both languages — TS via
  `@cendor/core/langchain`; keyless Entra-ID auth for Azure is in both too — TS via the
  `azureADTokenProvider` option.)
- **NER backends differ by language, and it's not parity.** Python uses Microsoft Presidio (spaCy
  models); TypeScript uses the optional `compromise` engine (`npm install compromise`) —
  synchronous (acttrace's tamper-evident append is sync, so an async transformer NER can't plug in),
  English-only, and with lower recall/precision. Treat the TS NER as an extra layer, not a sole PII
  control. **The Python `[ner]` extra installs Presidio + spaCy but not a language model** —
  install one once (`python -m spacy download en_core_web_sm`); `ner_available()` returns `True` only
  when both are present and `ner_redactor()` raises a clear error (never a pip auto-download) if the
  model is missing. `nerAvailable()` (TS) reports whether `compromise` is installed.
- **Docs code samples default to Python** where a tab pair isn't shown; the mapping rules above
  translate mechanically.
