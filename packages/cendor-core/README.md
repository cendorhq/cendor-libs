# cendor-core

The shared foundation for the Cendor stack: canonical types, provider-aware token counting,
an offline price table, one `instrument()` interception point, an in-process event bus, and
OpenTelemetry GenAI emitters. Tiny on purpose — it's the blast radius for every other tool.

**One `instrument()` call, every sibling tool observes the stream — no per-call wiring, offline by default.**

![PyPI](https://img.shields.io/pypi/v/cendor-core) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · usually installed transitively · `import cendor.core`

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

## Highlights

- **`instrument()`** — wrap any client once: **OpenAI** (Chat Completions + Responses API) **· Anthropic · Hugging Face** (`InferenceClient`) **· AWS Bedrock · Google Gemini** (`google-genai` + legacy `google-generativeai`) **· Ollama**, detected by *shape*; sync, async, **and streaming**; idempotent + additive. `instrument_tool()` does the same for tools.
- **Streaming is a context manager *and* an iterator** — the streamed value supports both `for chunk in stream` / `async for` **and** `with client…create(stream=True) as stream:` / `async with`, matching the SDK's own stream and unbreaking frameworks (e.g. LangChain) that consume streams via `with`. Usage/cost finalize exactly once.
- **Event bus** — `subscribe` / `emit`; **thread-safe within a process**; one failing subscriber never starves another.
- **Interceptor seam** — `add_interceptor` + `Reroute` / `MISS` powers replay (cassette) and reroute / block (tokenguard) **without a second patch point**.
- **Token counting, three tiers** — exact (`[tiktoken]`) / BPE-estimate (o200k for Claude/Gemini) / offline heuristic; `tokens.method(model)` reports which is active; `tokens.register()` plugs in a precise counter.
- **Reasoning-token accounting** — `Usage.reasoning_tokens` breaks out a reasoning/thinking model's internal reasoning (OpenAI `reasoning_tokens`, Gemini `thoughts_token_count`), non-streaming and streaming. A subset of `output_tokens`, so cost is unchanged; Gemini's separately-reported thoughts are folded into the output total.
- **Offline-first, refreshable prices** — bundled dated snapshot; `estimate() -> Decimal Money` (never `float`); optional `refresh(source="litellm"|"openrouter"|"azure")` from live no-auth sources, with `age_days()`/`is_stale()` staleness signals. Cached tokens are billed **once** (`cached ⊆ input`, normalized across providers), not at both the input and cached rate. A gateway-reported cost (e.g. OpenRouter's `usage.cost`) is preferred over the estimate and labeled `cost_reported` vs `cost_estimated`.
- **OpenTelemetry** — emit `gen_ai.*` spans, or `otel.ingest()` a managed runtime's spans onto the bus. Structural protocols (`Compressor` / `EvictionStrategy` / `Sink` / `Subscriber` / `Handle`) let the tools interlock without coupling. `Sink` now has optional `flush()`/`close()` lifecycle methods (write-only sinks still valid).
- **LangChain / LangGraph** — `cendor.core.langchain.CendorCallbackHandler` (optional extra `cendor-core[langchain]`) is the SDK-aligned way to observe a framework: attach it as a callback to record usage + **reasoning** + tool calls + a **root-run `trace_id`** across a whole agent, with no client touch. Recording-only (enforcement stays on the `instrument()` seam). For direct-SDK agents, `core.trace("run-id")` sets the same ambient `trace_id`.

Install `cendor-core[tiktoken]` for **exact** OpenAI counts (heuristic fallback otherwise), `[otel]` to emit spans, or `[langchain]` for the LangChain/LangGraph callback handler. Provider SDKs are always optional extras.

A rendered architecture diagram lives in [`docs/core.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/core.md) (GitHub renders Mermaid; PyPI shows code as text).

See [`docs/core.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/core.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-core/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
