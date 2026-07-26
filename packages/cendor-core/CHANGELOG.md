# Changelog — cendor-core

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.14.0] — 2026-07-26
**`core.trace()` groups your calls into one trace, and a governance row can finally name the agent it stopped.**

### `trace()` is a real span — the behaviour change to read before upgrading

`with trace("id"):` used to stamp an ambient id onto every `LLMCall`/`ToolCall` and nothing more, so
every call inside still arrived as its **own root span**: one logical unit of work became N unrelated
traces in any backend that groups by trace. Measured against Cendor Monitor on 2026-07-26, a scope
around a chat call *and* a tool call produced **two** traces sharing one id — one run, two rows, no
parent, its governance fanned out to both, and per-run governance counts doubled.

The scope now brackets its calls with a `cendor.trace <id>` span (instrumentation scope `cendor.core`,
carrying `cendor.run.id` and `cendor.scope="trace"`), so **one scope is one trace**, and each child
call carries a 1-based `cendor.step`. The ambient id is stamped exactly as before, so correlation by
`cendor.trace_id` is unaffected.

Nothing is emitted when there is nobody to emit to (no OpenTelemetry, no configured provider, or
`CENDOR_TELEMETRY=off`), and **no span is opened inside a cendor-sdk run** — that run already owns its
trace, so the calls attach to it rather than to a competing root. Nesting is a no-op for the inner
scope: one root per scope family.

**If your backend groups by trace id today and you want the old shape**, one switch restores it:
`CENDOR_TRACE_SPAN=off`, or `trace(id, span=False)` for a single scope.

### `ambient_attrs()` — so a governance record can name its actor

`apply_ambient` covers everything that *is* an event. A governance record is not: an audit entry or an
enforcement decision is built by `acttrace` / `tokenguard` / `guardrails`, which must not import the
SDK, and so had no way to learn which agent was acting. Measured: **13 of 386** governance rows named
their agent. `ambient_attrs()` is a **read** of the same registry — core still carries no identity of
its own — and core's own `governance.*` spans now use it, so a **budget block** (an event with no agent
field at all) stops being an anonymous row. An agent name is app-supplied configuration, not
input-derived text, so it does not breach the rule that keeps a guardrail `reason` off these spans.

### Provider-native agent identity, in the adapters

* `gen_ai.agent.id` is emitted on a call span whenever something stamped one — **never** hashed and
  never a placeholder. A name is a label (two apps can share one, and a rename loses that agent's
  history); an id is identity.
* **New `cendor.core.agent_ids`**: `bedrock_agent_scope(agent_id=…, agent_alias_id=…, session_id=…)`,
  `openai_assistant_scope(assistant_id=…, thread_id=…)` and the generic `agent_scope(...)`, mapping the
  ids those products already own onto `gen_ai.agent.id` / `gen_ai.conversation.id`.
* `cendor.core.foundry` now also maps its `agent_id` onto `gen_ai.agent.id` (it keeps stamping `agent`,
  so a dashboard grouping on the name dimension does not lose its rows).
* All three stay **attribution-only**: mapping identity does not make a server-side runtime's tokens or
  cost appear, and the docs say so.

## [1.13.0] — 2026-07-25
**Governance is now visible as ordinary telemetry — with no audit object and no `audit.*` vocabulary.**

Until now the only way a budget block or a guardrail verdict reached your backend was the *audit
mirror*, so seeing enforcement meant adopting the evidence library. Under the telemetry switch, the
decisions your stack makes are rendered as plain monitoring spans:

| Span | Attributes |
|---|---|
| `governance.budget_event` | `cendor.gov.type/action/budget/scope/model/to_model/projected_usd/cap_usd/projected_tokens/cap_tokens` + `cendor.trace_id` |
| `governance.guardrail_decision` | `cendor.gov.type/guardrail/stage/action/agent/tool` + `cendor.trace_id` |

Scope is `cendor.core` for a libs app; inside an SDK run, `cendor-sdk` renders the same events as
children of the run root (`cendor.sdk`), so the decision sits next to the steps it governed.

### Added
- The two renderings above on the bus→span emitter, duck-typed exactly like `acttrace` chains them
  (core imports no tool — rule 2).
- **`otel.governance_mirrored(on)` / `otel.governance_mirror_active()`** — `acttrace` refcounts a
  mirror that emits spans, and while one is live these ops renderings **stand down**: the chained
  `audit.*` spans are richer and must win, and an event must never render twice. A *custom* mirror
  (a SIEM sink) deliberately does not suppress them — nothing audit-shaped is on the wire then.

### Rule 6 (honesty), by construction
No `audit.*` span name, no `cendor.audit.*` attribute, nothing evidence-shaped: "audit" keeps meaning
the hash-chained file `verify()` checks. And **no `reason` string is emitted** — a guardrail's reason is
written by the rule, and by a judge *model* for `rules.llm_judge` (free text that can paraphrase the
payload; the URL rules embed the matched host). The audit chain — an artifact you declared — keeps
carrying it; these default-on spans do not. A test pins that no payload marker can reach a
`cendor.gov.*` attribute.

## [1.12.0] — 2026-07-25
**Telemetry now flows with zero telemetry code — and `CENDOR_TELEMETRY=off` turns it all off.**

⚠️ **This is a default-behaviour change.** If OpenTelemetry is installed (`pip install
"cendor-core[otel]"`) **and** your app configures a global tracer provider (`configure_azure_monitor()`,
a plain `set_tracer_provider`, an OTLP endpoint pointed at Cendor Monitor…), then after upgrading a
governed call arrives in **your** backend as a standard `gen_ai.*` span without a line of Cendor
telemetry code. You will see:

| What appears | From | Scope / names |
|---|---|---|
| `chat …` / `execute_tool …` span per governed call | this package — the emitter attaches itself at your first `instrument()` (or `otel.ingest()`) | `cendor.core`, standard `gen_ai.*` |

(`cendor-tokenguard` 1.6.0 adds the spend counters, `cendor-acttrace` 1.11.0 the `audit.*` mirror,
`cendor-sdk` 1.18.0 the run root — same switch, same default.)

Cendor still has **no endpoint, no exporter and no collector of its own**: it emits into the provider
*you* configured. With OpenTelemetry absent, or with no provider configured, behaviour is
byte-identical to 1.11.x — not one extra bus subscriber. Prompt/response **content stays opt-in**
(`otel.capture_content()`). No new identity: the app name is still the OTel resource's `service.name`.

**Turning it off / diagnosing it**
- `CENDOR_TELEMETRY=off` — process-wide, no code change; read per event, so it applies even if you
  export it late. `OTEL_SDK_DISABLED=true` (the standard switch) composes for free.
- `CENDOR_DEBUG_TELEMETRY=1` — one stderr line stating the mode, whether a provider was detected and
  what got wired. Silent otherwise: Cendor never nags an offline app.

### Added
- **`otel.telemetry_mode()`** — the effective mode from `CENDOR_TELEMETRY` (`"auto"` default | `"off"`;
  an unrecognised value is `auto`, noted once under `CENDOR_DEBUG_TELEMETRY=1`, because a typo must
  never silently disable telemetry).
- **`otel.provider_configured()`** — True once the app registered a real (non-proxy) global tracer
  provider. It never inspects exporters or endpoints.
- **`otel.live_spans_active()`** — whether an SDK `live_spans` scope is open in this context (the SDK
  reads it so an explicit scope always wins over its automatic one).
- **`otel.auto_telemetry_state()`** — a diagnostics dict (`mode`/`otel`/`provider`/`armed`/`emitting`/
  `manual`), for `cendor-init doctor` and tests.
- **Automatic span emitter.** `instrument()` / `otel.ingest()` arm **one** bus subscriber that stays
  dormant — re-checking the ~300 ns provider predicate per event — until a provider appears, then
  latches and renders. Attach order therefore never matters, and a provider configured *after* the
  first call is still caught. `use_span_emitter()` still works and **always wins**: a manual
  attachment detaches the automatic one, so an event is never rendered twice.

### Fixed
- **`use_span_emitter(tracer)` now honours an explicitly passed tracer when OpenTelemetry is not
  installed.** The `ImportError` guard ran first, so passing your own tracer (or a recording double)
  into an OTel-less environment silently subscribed nothing. The TypeScript port never had this
  asymmetry.

## [1.11.1] — 2026-07-24
**Fix: the openai-agents adapter now actually stamps the agent name on live calls.** Found by the black-box testsuits live probe: the OpenAI Agents SDK runs each model call in an async context **isolated** from the `RunHooks`, so the `ContextVar` set in `on_agent_start` (or `on_llm_start`) never reached the captured `LLMCall` — the name was silently dropped live (the offline fixture passed because it drove hooks + call in one context). `instrument()` *did* capture the call with real usage, so "the calls ride the standard client" always held; only the name was missing.

### Fixed
- **`cendor.core.openai_agents`** now tracks the active agent in a **process-wide holder** (updated by the hooks, read live at event construction) instead of a `ContextVar` — so the framework's agent name reaches the model call for real (verified live). **Honest limit:** correct for sequential runs + handoffs (the common case); concurrent `Runner.run()` in the same process may cross-attribute during overlap (per-run scoping is impossible — the SDK isolates the call's context from the hooks). Run concurrent multi-agent workloads in separate processes. `cendor.core.foundry` is unaffected (its `foundry_agent_scope` is a synchronous callback wrap — the scope *is* the call's context).

## [1.11.0] — 2026-07-23
**Framework agent-name adapters** — two optional integrations that source a *third-party framework's* agent identity onto the bus, so the monitor's Agents page fills for framework-driven stacks. Additive; nothing changes unless you attach one (importing an adapter registers no ambient provider — core's zero-provider fast path is untouched). Core carries no identity of its own (Raghav's locked principle) — the framework owns the name; these adapters carry it.

### Added
- **`cendor.core.openai_agents.CendorAgentHooks`** (extra `[openai-agents]`) — a `RunHooks` you pass to the OpenAI Agents SDK's `Runner.run(..., hooks=…)`. On each agent turn it stamps the framework's agent name via a scoped ambient provider (set at agent start / handoff, cleared at end); the agent's model calls ride the standard OpenAI client, so `instrument()` still captures tokens/cost/streaming — this supplies *only* the name (GLR-11c). Mirrors the `cendor.core.langchain` handler; never-overwrite.
- **`cendor.core.foundry`** (`observe_foundry_agents(client)` + `foundry_agent_scope(agent_id, thread_id)`; extra `[foundry]`) — a correlation adapter for Azure AI Foundry Agents. It wraps `client.runs.{create,create_and_process,stream}` (duck-typed on `.runs`, sync + async) to stamp `agent` + `conversation_id` for the run's duration. **Attribution only** — the model runs server-side, so there is no per-step token/cost capture here (a documented honest limit). Importing this module needs no Azure SDK (it wraps a client you pass in).

## [1.10.0] — 2026-07-23
The per-chunk **stream-observer seam** + visible-thinking stream estimation + two Python capture repairs. Additive; nothing changes unless an observer is registered.

### Added
- **`add_stream_observer(fn)` / `remove_stream_observer(fn)`** — register a per-chunk observer `fn(call, delta_text, delta_thinking)` on every instrumented stream. **Raising aborts the stream** (interceptor discipline): core closes the underlying provider stream, finalizes the `LLMCall` once with the partial (estimated) usage, and re-raises to the consumer. Zero observers ⇒ one truthiness/length check per chunk (streaming hot path untouched). This is the generic seam `cendor-tokenguard`'s mid-stream budget breaker (`budget(on_exceed="break")`) rides — core learns no budget vocabulary.
- **Bedrock `converse_stream` capture (Python)** — a Bedrock client's `converse_stream` (no `stream=` kwarg; the event iterable arrives as the `"stream"` member of a dict response) is now wrapped, priced, and recorded like any stream.

### Changed
- **Streamed usage estimation now counts *visible* thinking** — Anthropic `thinking_delta`, Ollama `message.thinking`, OpenAI-compat `reasoning_content`, and Bedrock `reasoningContent` are folded into output and surfaced as reasoning. Narrows the documented limit from "can't see thinking" to "can't see *hidden* thinking" (OpenAI-native/Gemini reasoning still never reaches the wire).
- **HF streamed-usage injection is signature-gated** — `stream_options={"include_usage": True}` is injected for Hugging Face only when the installed `huggingface_hub`'s `chat_completion` explicitly accepts it (never blind — avoids a 4xx / `TypeError` on older hubs). OpenAI is unchanged.

### Fixed
- **Async-detect repair (Python).** A sync-looking client method that actually returns an awaitable (a misdetected async client — `iscoroutinefunction()` was `False`) now has its usage captured via an awaited continuation, instead of silently losing usage on the un-awaited coroutine. A truly sync client never returns an awaitable, so there are zero false positives.

## [1.9.0] — 2026-07-22
The ambient metadata seam — the one core-owned pre-emit capture point for run context. Additive; nothing changes unless a provider is registered.

### Added
- **`add_ambient_provider(fn)` / `remove_ambient_provider(fn)`** — register a `(event) -> dict | None` provider that runs at every event's construction (the caller's synchronous frame, before interceptors), merging its metadata onto `event.metadata` with never-raise / never-overwrite / registration-order semantics and a zero-provider single-length-check fast path. This is how a library (or app) attaches agent / conversation id / budget frames / decision id / cassette session at the moment it is unconditionally correct, instead of re-reading contextvars at bus-delivery time (which breaks for streams finalized outside their scope, context-losing layers, subscriber order, concurrent runs, and Python generators that leak run scopes into the consumer).
- **`otel.ingest()` stamps the ambient `trace_id`** at construction, so an ingested call joins its run.
- **`otel.use_span_emitter()` maps `metadata["agent"]` → `gen_ai.agent.name`** — a libs-only app self-identifies an agent (via a provider or the LangChain handler) with no SDK.
- **The LangChain callback handler stamps an agent/chain/LangGraph-node name** into `metadata["agent"]` (explicit `metadata["agent"]` wins).

## [1.8.0] — 2026-07-21
Estimated-usage provenance on emitted spans — the emission-truth half of Monitor v5 (G-V4-3). Additive; nothing changes unless a streamed call's token count was recovered by offline estimate.

### Added
- **`cendor.usage_estimated="true"`** on an emitted `chat` span (the libs-only `otel.use_span_emitter()`) when the streamed call reported no usage and the count was recovered by `_estimate_stream_usage` (`metadata["usage_estimated"]`). Truth = the product: a monitor can now render those tokens as *est.* rather than the provider's billed figure. Stamped only when set (a real, provider-reported count leaves the span unflagged).

## [1.7.0] — 2026-07-20
Opt-in content capture, a libs-only span emitter, and TTFT — the emission half of the Cendor journey console (Monitor v3).

### Added
- **Opt-in content capture (OFF by default)** — `otel.capture_content(mode="span", mask=…, max_bytes=…)` and the standard `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` env var turn on export of prompts/responses/thinking/tool values onto the semconv content span attributes (`gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.system_instructions`, JSON strings). A `mask` (`messages -> messages`) scrubs before export (fail-closed if it raises); `max_bytes` caps each attribute with a truncation marker. **Content never enters the acttrace evidence chain or its mirror** (rule 6). Helpers `otel.content_attrs(...)` / `otel.tool_content_attrs(...)` build the attribute dicts.
- **`otel.response_messages(call)`** — best-effort per-provider parse of assistant output into text + **thinking** parts (Anthropic `thinking` blocks, OpenAI Responses reasoning summaries, Gemini `thought` parts, Bedrock `reasoningContent`, Ollama `thinking`, DeepSeek `reasoning_content`) — the content the provider `parse()` drops. Content only, opt-in.
- **`otel.use_span_emitter()`** — an opt-in bus→span subscriber that emits a `chat`/`execute_tool` semconv span per `LLMCall`/`ToolCall`, so a **libs-only app (no SDK)** lights up a trace-based monitor. Honors content capture; defers to an active SDK `live_spans` context (no double spans) via `otel.enter_live_spans()`/`exit_live_spans()`.
- **TTFT** — streamed calls now stamp `metadata["ttft_ms"]` (first-chunk latency), surfaced as `cendor.ttft_ms` on emitted chat spans. Replayed streams are excluded.

## [1.6.0] — 2026-07-14
Embeddings capture, Usage arithmetic, and a survive-refresh price registry — the core half of the SDK↔lib inheritance fixes.

### Added
- **`instrument()` now captures `embeddings.create`** on openai-shaped clients (OpenAI + Azure-via-openai): the pre-flight interceptor pass runs — budget block/clamp and guard redact-before-send now apply to embedding calls (a `Reroute(messages=…)` maps back to the raw `input` shape) — and the emitted `LLMCall` carries `metadata["embedding"] = True`, usage from `response.usage`, and cost from the price table. Sync + async. Embeddings leave the documented capture-gaps list for openai-shaped clients.
- **`Usage` arithmetic** — `Usage.__add__` (supports `sum(...)`) and `sum_usage(iterable)`, exported from `cendor.core`. Field-complete **by construction** (iterates the dataclass fields), so a future `Usage` field can never silently vanish from an aggregate.
- **`prices._register(model, rates)`** — the contractual programmatic write hook (the seam `cendor.sdk.register_model_price` writes through; underscore-named to stay out of the end-user API, but stable within 1.x). Registrations **survive `refresh()`** — re-applied after every table swap instead of being dropped.
- The bundled price snapshot gains the OpenAI embedding rows (`text-embedding-3-small` $0.02/1M · `text-embedding-3-large` $0.13/1M · `text-embedding-ada-002` $0.10/1M — verified on the official model pages), so USD budgets bind on embedding calls out of the box.

## [1.5.2] — 2026-07-11
Model-currency patch: today's models price correctly out of the box.

### Changed
- **Price snapshot regenerated for the current model generation** (`_updated` 2026-07-11; every
  rate verified against the official provider pricing pages): adds the OpenAI gpt-5.x line
  (5.6-sol/terra/luna, 5.5, 5.5-pro, the 5.4 family, 5.3-codex, 5.2, 5.1), Anthropic
  claude-fable-5 / claude-mythos-5 / claude-sonnet-5 (listed at the standard rate effective
  2026-09-01; the intro rate through 2026-08-31 is noted in `_note`) / opus-4-7/-4-6/-4-5,
  Gemini 3.x (3.5-flash, 3.1-pro-preview, 3.1-flash-lite, 3-flash-preview), and xAI grok-4.3 /
  grok-4.5. **claude-haiku-4-5 corrected** to the official $1/$5 (+ $0.10 cache read / $1.25 5m
  write) — the old row carried Haiku 3.5 rates. Gemini 2.5 cache-read rates updated
  ($0.125 / $0.03). Dead rows removed: gemini-2.0-flash (shut down 2026-06-01), gemini-1.5-pro.
  gpt-4o / gpt-4.1 / o-series stay as legacy rows.
- **Wire-level model ids now price at lookup**: Bedrock modelIds (`anthropic.…-v1:0`,
  `us.`-region profiles) and dated Anthropic / OpenAI snapshot ids resolve to their base row
  instead of yielding `cost=None`. Unknown models still raise `UnknownModelError` — normalization
  never invents a price.

### Docs
- The token-exactness claim is scoped honestly: `tiktoken` (0.13.0) ships no gpt-5.x mapping yet,
  so gpt-5.x counts via the `o200k` BPE proxy (`method()` reports `bpe-estimate` and upgrades
  automatically when a mapping ships). New honest-limits entry + trap-table row for the
  entrypoints `instrument()` does not capture (`chat.completions.parse` / `responses.parse`,
  Anthropic's `messages.stream()` helper + `tool_runner`, Batch APIs, embeddings).

## [1.5.1] — 2026-07-11
AI-assistant onboarding: inline Type Teach ships inside the package, plus the bundled integration guide. No runtime behavior change for correct code.

### Added
- Inline `@example` + a correct-shape signature on public symbols, so your editor's language server (and agent-mode assistants) is handed the right call as you type — the wrong shape becomes a type error whose message states the right one.
- `INTEGRATION.md` is now bundled in the installed package — a one-screen "call Cendor correctly" guide. Full trap sheet: https://cendor.ai/docs/for-ai-assistants

## [1.5.0] — 2026-07-10
Deep-QA fixes: token accuracy for the open/hosted-model class, and honest top-level exports.

### Changed
- **Non-OpenAI / unrecognized models now count via the `o200k` BPE proxy, not the character heuristic.** Any model whose family resolves to `default` — llama, mistral, deepseek, qwen, new o-series ids (`o5-mini`), and OpenAI fine-tunes (`ft:gpt-4o:*`) — routes through tiktoken's `o200k_base` estimate (reported as `bpe-estimate`), exactly like Claude/Gemini, instead of the rough char heuristic. **This changes token counts** for the whole open/hosted-model class — hence a minor — and every `tokenguard` budget / `clamp` that calls `tokens.count` inherits the correction. The o-series match is generalized (`^o\d`, so new ids don't fall through) and an `ft:` fine-tune strips to its base model, counting `exact`. The character heuristic is now only ever reached if tiktoken fails to import.

### Added
- **`add_interceptor`, `remove_interceptor`, and `MISS` are re-exported from `cendor.core`** (top-level), matching `core.md` and `@cendor/core`'s top-level exports — no more importing from the private `cendor.core.instrument`.

## [1.4.0] — 2026-07-08
### Changed
- **`tiktoken` is now a required dependency** (was the optional `[tiktoken]` extra), so a plain
  `pip install cendor-core` counts OpenAI tokens **exactly** out of the box — and therefore reports
  truthful cost/budget numbers by default. Truthful token counts are the product, not an opt-in; this
  brings Python in line with `@cendor/core`, which already hard-deps `js-tiktoken`. The character/
  subword heuristic remains in the code **only as a defensive fallback** if `tiktoken` ever fails to
  import (a broken/partial install) — it is no longer the path a normal install silently lands on.
  `tiktoken` is fully offline (no network, no account), so this preserves the local-first guarantee.
  The `[tiktoken]` extra is kept as a back-compat no-op so existing `cendor-core[tiktoken]` pins keep
  resolving. No API change: `tokens.count`/`method`/`register` are unchanged.

## [1.3.1] — 2026-07-05
### Changed
- Repository moved to `github.com/cendorhq/cendor-libs`; `[project.urls]` and the offline price-snapshot refresh URL (`prices.SNAPSHOT_URL`) now point at the new location. No API or behavior change.

## [1.3.0] — 2026-07-05
### Added
- **Hugging Face detection in `instrument()`** — a `huggingface_hub.InferenceClient` is now recognized by its `chat_completion` method and wrapped, emitting an `LLMCall` attributed to `huggingface` with usage/cost captured. The response is OpenAI-shaped, so usage extraction and streamed-text handling reuse the OpenAI path. Purely additive and backward-compatible: clients without a `chat_completion` method are unaffected, and the check runs *before* the OpenAI-compat detection so an `InferenceClient` that also exposes `chat.completions.create` is still attributed to `huggingface` (not `openai`). Enables `cendor-sdk`'s HuggingFace provider to capture governed usage/cost/audit.

## [1.2.0] — 2026-07-05
### Added
- **`cendor.core.langchain.CendorCallbackHandler`** — an optional LangChain/LangGraph callback handler (the SDK-aligned way to observe a framework) that records **usage + reasoning + cached** tokens (from LangChain's `usage_metadata`), prices each call offline, emits normalized `LLMCall`/`ToolCall` on the bus, and correlates a whole `agent.invoke` — across its nodes, react loop, and tools — under one **root-run `trace_id`**. **No client touch**, so it sidesteps the `with_raw_response` usage loss and the streaming context-manager crash. **Recording-only** (post-call): enforcement stays on the `instrument()` seam. Gated by a new optional extra `cendor-core[langchain]` (`langchain-core>=0.3`); importing the module without it raises a clear `ImportError`. Keeps core dependency-light — nothing new is a hard dependency.
- **`trace()` / `current_trace_id()` correlation hook** — an ambient `contextvars` `trace_id` stamped onto every `LLMCall`/`ToolCall` emitted inside a `with trace("run-id"):` block, so **direct-SDK** agents get the same run correlation the LangChain callback path derives from `parent_run_id`. Default is `""` (no behaviour change) unless set. A hook, not an orchestrator.
- **`Sink` protocol gained optional `flush()` / `close()` lifecycle methods.** `write(entry)` remains the **only required** member (so `runtime_checkable` still matches write-only sinks); a sink *may* additionally implement `flush()` (block until buffered records are durable) and `close()` (flush + release resources), which callers invoke via `hasattr`/`getattr` guards. This is the seam `tokenguard.sinks.QueueSink` uses to move durable I/O off the hot path. Purely additive.
- **Streamed responses are now a context manager *and* an iterator.** `instrument()`'s streaming proxy (sync and async) supports both `for chunk in stream` / `async for` **and** `with client…create(stream=True) as stream:` / `async with`, matching the provider SDK's own stream surface. This fixes a `TypeError: 'generator' object does not support the context manager protocol` crash when a framework (e.g. `langchain_openai`) consumes a streamed completion via `with`. The `LLMCall` still finalizes exactly once (on exhaustion, early `close()`, or block exit), unknown attributes (`.response`, `.close()`, …) forward to the underlying SDK stream, and replayed streams (`cassette`) gained the same surface. Additive and backward-compatible — existing `list(stream)` / `async for` iteration is unchanged.

## [1.1.0] — 2026-07-04
### Added
- **`Reroute(messages=…)`** — an interceptor can now rewrite the outbound **messages** (not just the model), mapped to each provider's own kwarg (`messages` / `input` / `contents`) and reflected on `call.messages`. Applies to sync, async, and streaming calls. This is the seam `acttrace`'s `guard()` uses for redact-before-send. Additive and backward-compatible — existing `Reroute(model=…)` / `Reroute(**kwargs)` behaviour is unchanged.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-core` — the shared foundation for the Cendor stack: canonical types, provider-aware token counting, an offline price table, a single `instrument()` interception seam, an in-process event bus, and OpenTelemetry GenAI emitters. Kept tiny on purpose; it's the blast radius for every other tool.
- **`instrument()`** wraps any client once — OpenAI (Chat Completions **and** the Responses API), Anthropic, AWS Bedrock, Google Gemini (`google-genai` and legacy `google-generativeai`), and Ollama — detected by *shape*; sync, async, and streaming; idempotent and additive. `instrument_tool()` does the same for tools.
- **Event bus** — `subscribe` / `emit`, thread-safe within a process, where one failing subscriber never starves another.
- **Interceptor seam** — `add_interceptor` with `Reroute` / `MISS`, powering replay (cassette) and reroute/block (tokenguard) without a second patch point.
- **Token counting, three tiers** — exact (`[tiktoken]`), an o200k BPE estimate (Claude/Gemini), or an offline heuristic; `tokens.method(model)` reports which path is active and `tokens.register()` plugs in a precise counter.
- **Reasoning-token accounting** — `Usage.reasoning_tokens` breaks out a thinking model's internal reasoning (a subset of `output_tokens`, so cost is unchanged), non-streaming and streaming.
- **Offline-first, refreshable prices** — a bundled dated snapshot; `estimate() -> Decimal` money (never `float`); optional `refresh(source="litellm"|"openrouter"|"azure")` from live no-auth sources, with `age_days()` / `is_stale()` staleness signals. Cached tokens are billed once, and a gateway-reported cost is preferred over the estimate (`cost_reported` vs `cost_estimated`).
- **OpenTelemetry** — emit `gen_ai.*` spans, or `otel.ingest()` a managed runtime's spans onto the bus.
- **Structural protocols** — `Compressor`, `EvictionStrategy`, `Sink`, `Subscriber`, and `Handle` let the tools interlock without coupling.
