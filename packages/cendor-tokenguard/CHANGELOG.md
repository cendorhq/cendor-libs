# Changelog — cendor-tokenguard

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.2.0] — 2026-07-19
Observability: pre-flight budget actions are now visible to your audit trail and metrics backend. Backward-compatible.

### Added
- **`BudgetEvent` on the bus.** Each pre-flight budget action — `blocked`, `downgraded`, or `clamped` — is emitted on the `cendor.core` bus. A blocked call never reaches the bus as an `LLMCall` (it's refused before it runs), so this event is the only signal the breaker fired; `acttrace` chains it as a `budget_event`, and an OpenTelemetry mirror can alert on it.
- **`sinks.OTelSink` attribution dimensions.** The spend counters are now dimensioned by the active `track(...)` tags (feature / user / …) as well as `model`, so a metrics backend can break spend down by attribution. Pass `OTelSink(tags=False)` for model-only counters to bound metric cardinality. See [Observability](https://cendor.ai/docs/observability).

## [1.1.3] — 2026-07-11
AI-assistant onboarding: inline Type Teach ships inside the package, plus the bundled integration guide. No runtime behavior change for correct code.

### Added
- Inline `@example` + a correct-shape signature on public symbols, so your editor's language server (and agent-mode assistants) is handed the right call as you type — the wrong shape becomes a type error whose message states the right one.
- `INTEGRATION.md` is now bundled in the installed package — a one-screen "call Cendor correctly" guide. Full trap sheet: https://cendor.ai/docs/for-ai-assistants

## [1.1.2] — 2026-07-10
Deep-QA fix.

### Fixed
- **`on_exceed="clamp"` now always injects the provider output ceiling** (`max_completion_tokens` / `max_tokens` = the tokens left in the budget) on every call under a token budget — not only when the 256-token reserve heuristic would breach. A single surprise-long call can no longer overshoot the `tokens=` cap while headroom exists. The input-alone-exceeds → hard-block fallback and a caller's own tighter cap are unchanged.

## [1.1.1] — 2026-07-05
### Changed
- Repository moved to `github.com/cendorhq/cendor-libs`; `[project.urls]` updated. No API or behavior change.

## [1.1.0] — 2026-07-05
### Added
- **`sinks.QueueSink(inner, *, max_queue=None)`** — wrap any spend sink so its writes run on a background daemon thread, keeping durable I/O **off the model call's hot path** (the bus runs subscribers inline, so a SQLite/OTel/file sink otherwise adds its latency to every call). `write()` enqueues and returns immediately; a single worker drains the inner sink **in order**. `flush()` blocks until drained; `close()` flushes, stops the worker, and closes the inner sink (also a context manager) — call one before exit for durability, since the worker is a daemon. `max_queue=` applies back-pressure when full (never silently drops a row). Uses the optional `Sink.flush()`/`Sink.close()` lifecycle methods (see `cendor-core` 1.2.0). Additive — existing sinks and `use_sink()` are unchanged.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-tokenguard` — stop runaway LLM bills and get per-feature / per-user cost attribution for free, with one decorator and one context manager. No dashboard, no account, no infra; it rides core's `instrument()` seam and never patches your client.
- **Pre-flight circuit breaker** — `on_exceed="block"` raises **before** an over-budget call runs; `"downgrade"` reroutes to a cheaper model pre-flight; `"truncate"` degrades; `"raise"` stops a runaway loop; or call your own function.
- **Reasoning models, handled** — `on_exceed="clamp"` injects the provider's own token ceiling (`max_completion_tokens` / `max_tokens`) sized to the remaining budget, so a call is capped **server-side** instead of overspending; `report()` breaks out `reasoning_tokens`.
- **Decorator *and* context manager** — budgets **nest** (an inner downgrade never masks an outer hard cap), and config is validated at creation (a typo'd `on_exceed` or a map-less `downgrade` is a `ValueError`, never a silent no-op).
- **Cost attribution, free** — `track(feature=…, user_id=…)` tags ambient spend via `contextvars` (sync + async); `report(group_by=[…])` shows where the money went.
- **Cost as a test assertion** — `report().assert_under(usd=0.05, feature="search")`.
- **Pre-flight projection** — `estimate(model, messages)` prices a call *without making it*.
- **Durable + bounded** — pluggable `use_sink(tokenguard.sinks.SQLiteSink / OTelSink)`, plus a FIFO-bounded in-memory buffer (`configure(max_records=…)`, `dropped()`).
- **No silent USD blind spots** — an unpriced model records `$0`, so tokenguard warns once (`UnpricedModelWarning`), counts them in `unpriced_calls()`, and `configure(on_unpriced="raise")` makes `block` reject them; token caps enforce regardless of price.
- **Thread-safe, with one caveat** — the spend buffer and `SQLiteSink` are lock-guarded; budgets/tags are `ContextVar`-based (asyncio tasks inherit them, a plain `threading.Thread` does not — carry them with `contextvars.copy_context()`).
