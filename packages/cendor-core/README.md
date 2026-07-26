# cendor-core

The shared foundation for the Cendor stack: canonical types, provider-aware token counting,
an offline price table, one `instrument()` interception point, an in-process event bus, and
OpenTelemetry GenAI emitters. Tiny on purpose — it's the blast radius for every other tool.

**One `instrument()` call, every sibling tool observes the stream — no per-call wiring, offline by default.**

![PyPI](https://img.shields.io/pypi/v/cendor-core) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · usually installed transitively · `import cendor.core`

Using an AI coding assistant? `npx @cendor/init` (TS) / `uvx cendor-init` (Python) wires it up — or point it at [cendor.ai/docs/for-ai-assistants](https://cendor.ai/docs/for-ai-assistants).

```python
from cendor.core import tokens, prices, instrument, bus

# Count tokens and price a call — fully offline, no API key, no network:
n = tokens.count([{"role": "user", "content": "Summarize the attached report in 3 bullets."}],
                 model="claude-opus-4-8")
cost = prices.estimate("claude-opus-4-8", input_tokens=n, output_tokens=200)

# Instrument any client once; tools subscribe to the normalized event stream:
@bus.subscribe
def on_call(call):                   # normalized LLMCall with usage + cost
    print(call.provider, call.model, call.cost)

client = instrument(openai_or_anthropic_client)   # idempotent, additive · sync · async · streaming
```


## Telemetry: it flows (and `CENDOR_TELEMETRY=off` stops it)

Since **1.13.0**, with OpenTelemetry installed and a provider configured **by your app**, core emits
`gen_ai.*` spans for every governed call as soon as you call `instrument()` — plus `governance.*` spans
for the budget/guardrail decisions the other libraries make. No emitter to attach, no exporter to
install: core has **no endpoint of its own** and emits into your provider. `CENDOR_TELEMETRY=off` turns
it off process-wide; `CENDOR_DEBUG_TELEMETRY=1` prints one line saying what was detected; `otel.telemetry_mode()` / `provider_configured()` let
you check the state yourself. With OpenTelemetry absent, nothing is subscribed and behaviour is
byte-identical.

## Group a unit of work: `core.trace()` (1.14.0)

Several calls that *are* one unit of work — a retrieval, a chat, a tool — belong in one trace:

```python
with trace("nightly-sweep"):
    client.chat.completions.create(...)
    client.chat.completions.create(...)
```

Since **1.14.0** the scope opens a real `cendor.trace <id>` parent span, so its calls become children
with a 1-based `cendor.step` — **one scope, one trace**. Before 1.14.0 it only stamped an ambient id, so
every call inside still arrived as its own root span; the ambient id is unchanged, so correlation by
`cendor.trace_id` is unaffected. Nothing is emitted with no provider configured or under
`CENDOR_TELEMETRY=off`, and **no span is opened inside a cendor-sdk run** (that run owns its trace). If
your backend groups by trace id today, `CENDOR_TRACE_SPAN=off` restores the old shape.

## Agent identity, only when you have it (1.14.0)

`gen_ai.agent.id` is emitted whenever something stamped one — an SDK `Agent(id=…)`, or an adapter for a
product that owns a real id: `cendor.core.agent_ids.bedrock_agent_scope` / `openai_assistant_scope` /
the generic `agent_scope`, and `cendor.core.foundry`. **No id ⇒ the attribute is omitted** — never a
hash of the name, never a placeholder. A name is a label (two apps can share one, and a rename loses
history); an id is identity. These scopes are **attribution-only**: mapping identity does not make a
server-side runtime's tokens or cost appear.

## Highlights

- **`instrument()`** — wrap any client once: **OpenAI** (Chat Completions + Responses API + **Embeddings**, since 1.6.0) **· Anthropic · Hugging Face** (`InferenceClient`) **· AWS Bedrock · Google Gemini** (`google-genai` + legacy `google-generativeai`) **· Ollama**, detected by *shape*; sync, async, **and streaming**; idempotent + additive. Embedding calls carry `metadata["embedding"]` and ride the same pre-flight interceptor pass (budgets can block, guards can redact). `instrument_tool()` does the same for tools.
- **Streaming is a context manager *and* an iterator** — the streamed value supports both `for chunk in stream` / `async for` **and** `with client…create(stream=True) as stream:` / `async with`, matching the SDK's own stream and unbreaking frameworks (e.g. LangChain) that consume streams via `with`. Usage/cost finalize exactly once.
- **Event bus** — `subscribe` / `emit`; **thread-safe within a process**; one failing subscriber never starves another.
- **Interceptor seam** — `add_interceptor` + `Reroute` / `MISS` powers replay (cassette) and reroute / block (tokenguard) **without a second patch point**.
- **Token counting, exact by default** — `tiktoken` is a required dependency, so OpenAI counts are exact out of the box (Claude/Gemini use its `o200k` BPE as a close estimate); a character heuristic remains only as a defensive fallback if `tiktoken` fails to import. `tokens.method(model)` reports which tier is active; `tokens.register()` plugs in a precise counter.
- **Reasoning-token accounting** — `Usage.reasoning_tokens` breaks out a reasoning/thinking model's internal reasoning (OpenAI `reasoning_tokens`, Gemini `thoughts_token_count`), non-streaming and streaming. A subset of `output_tokens`, so cost is unchanged; Gemini's separately-reported thoughts are folded into the output total.
- **Offline-first, refreshable prices** — bundled dated snapshot; `estimate() -> Decimal Money` (never `float`); optional `refresh(source="litellm"|"openrouter"|"azure")` from live no-auth sources, with `age_days()`/`is_stale()` staleness signals. Cached tokens are billed **once** (`cached ⊆ input`, normalized across providers), not at both the input and cached rate. A gateway-reported cost (e.g. OpenRouter's `usage.cost`) is preferred over the estimate and labeled `cost_reported` vs `cost_estimated`.
- **OpenTelemetry** — emit `gen_ai.*` spans, or `otel.ingest()` a managed runtime's spans onto the bus. Structural protocols (`Compressor` / `EvictionStrategy` / `Sink` / `Subscriber` / `Handle`) let the tools interlock without coupling. `Sink` now has optional `flush()`/`close()` lifecycle methods (write-only sinks still valid).
- **Framework adapters** — when your app runs under a third-party framework, a small adapter carries the *framework's* agent name onto the bus (core carries no identity of its own): **LangChain / LangGraph** `cendor.core.langchain.CendorCallbackHandler` (extra `[langchain]`, recording-only — usage + reasoning + tools + root-run `trace_id`); the **OpenAI Agents SDK** `cendor.core.openai_agents.CendorAgentHooks` (extra `[openai-agents]` — the agent's calls ride the standard OpenAI client, so `instrument()` still captures tokens/cost/streaming; this supplies only the name); and **Azure AI Foundry Agents** `cendor.core.foundry` (extra `[foundry]` — stamps `agent` + `conversation_id`, **attribution-only** since the model runs server-side). For direct-SDK agents, `core.trace("run-id")` sets the same ambient `trace_id`.

Exact OpenAI token counts ship **by default** (`tiktoken` is a required dependency — truthful counts are the product, not an add-on). Optional extras: `[otel]` to emit spans, `[langchain]` for the LangChain/LangGraph callback handler, `[openai-agents]` for the OpenAI Agents SDK hooks, `[foundry]` for the Azure AI Foundry correlation adapter; provider SDKs are always optional extras.

A rendered architecture diagram lives in [`docs/core.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/core.md) (GitHub renders Mermaid; PyPI shows code as text).

See [`docs/core.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/core.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-core/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
