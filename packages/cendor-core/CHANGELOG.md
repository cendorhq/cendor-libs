# Changelog — cendor-core

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

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
