# Changelog — cendor-tokenguard

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.6.1] — 2026-07-26
**Fix: `on_exceed="clamp"` no longer breaks an OpenAI call that already sets `max_tokens`.**

OpenAI accepts *either* `max_completion_tokens` (the newer name, required by reasoning models) or the
older `max_tokens`, and **rejects both together** with a 400. `clamp` read only
`max_completion_tokens` as the caller's existing cap and injected that kwarg regardless — so a plain
call with `max_tokens=4` inside a `budget(tokens=…, on_exceed="clamp")` scope returned

```
400 — Setting 'max_tokens' and 'max_completion_tokens' at the same time is not supported
```

Two problems in one: the caller's own cap was ignored (so the clamp always injected), and the injected
kwarg collided with theirs — a call that worked a moment earlier started failing the instant a clamp
budget was added around it. The clamp now **reuses whichever name the caller used**, which is what
`_projected_output` already did when reading the cap. Found live while seeding the monitor fit-gap
verification.

## [1.6.0] — 2026-07-25
**Spend reaches your backend with zero telemetry code** (see `cendor-core` 1.12.0 for the switch).

### Added
- **An internal OpenTelemetry spend tap.** When telemetry is on, every priced spend row is also written
  to an internal `OTelSink`, so `gen_ai.client.token.usage` / `.cost.usd` / `.reasoning.token.usage`
  (dimensioned by `model` **and** your `track(...)` tags) appear in the backend you configured without
  `use_sink(sinks.OTelSink())`. `CENDOR_TELEMETRY=off` disables it; without OpenTelemetry it is inert.
- **Your `use_sink` slot is untouched by it** — the tap is *additive*, beside the slot, precisely
  because `use_sink` **replaces**: routing automatic export through it would have meant a later
  `use_sink(SQLiteSink(...))` silently switching backend spend off.
- **No double counting on upgrade.** If your own sink already *is* an `OTelSink` (or a `QueueSink`
  wrapping one — what the current docs show), the tap stands down, so counters stay 1×.

## [1.5.1] — 2026-07-24
QueueSink drop observability + a docstring fix. Backward-compatible.

### Added
- **`QueueSink` drop observability** — an optional `on_drop_error(exc, entry)` constructor callback and a `dropped_rows` counter. When the wrapped inner sink's `write` raises (disk full, DB locked), the offending row is dropped so the failure can't kill the background drain worker — and now that drop is *counted* and optionally surfaced, instead of being silently swallowed. A broken callback is swallowed too (it can't kill the worker either).

### Fixed
- The module docstring's stale "Enforcement model (v0)" wording now describes the shipped three-point model — pre-flight (`estimate` + `clamp`/`downgrade`), mid-stream (`break`), and post-flight (bus record + breaker). Documentation only; no behavior change.

## [1.5.0] — 2026-07-23
Mid-stream budget breaker (`on_exceed="break"`) + nested-provider `clamp`. Backward-compatible.

### Added
- **`on_exceed="break"`** — a mid-stream budget breaker. It rides `cendor-core`'s new per-chunk stream-observer seam to cut a streamed call the instant its running output estimate (visible text + visible thinking) crosses the remaining `tokens=`/`usd=` budget: you keep the partial output already yielded, the provider bills to the cut (~one chunk + one RTT past — it stops the meter, it does not un-bill the provider), and the settled usage is an estimate flagged `usage_estimated`. USD headroom is converted to an integer token allowance once per stream (Decimal off the hot path); `reasoning_reserve` cuts early on hidden-thinking models. It also acts as a post-flight cumulative gate (like `"raise"`) for non-streamed calls, and emits a `BudgetEvent(action="broken")`.

### Changed
- **`on_exceed="clamp"` now injects the ceiling for more providers** — nested Bedrock `inferenceConfig.maxTokens` and Ollama `options.num_predict` (copy-on-write merged), plus a plain-**dict** Gemini `config.max_output_tokens`. A typed Gemini `GenerateContentConfig` can't be safely merged and still falls back to a hard block, as before (its `max_output_tokens` also does not bound hidden thinking — see docs).
- Requires `cendor-core >= 1.10` (the stream-observer seam).

## [1.4.0] — 2026-07-22
Streamed spend drained out of scope now accrues, enforces, and attributes (Bug A fix).

### Fixed
- tokenguard captures the active budget frames (by reference) + attribution tags **at call initiation** via the `cendor-core` ambient seam, instead of re-reading them at bus-delivery time. A streamed call whose stream is drained **after** the `budget()` / `track()` scope exits now still accrues spend, enforces the budget, and attributes by tag — previously that spend was silently lost, which also let a cumulative cap under `on_exceed="block"` be overrun (every call in a loop of streamed calls was judged against `spent=0`).

### Added
- **`BudgetEvent.trace_id`** — the run/trace id of the call the action guarded, so a monitor can join a budget action back to its run (`acttrace` copies it into the audit entry's `run_id`).

### Changed
- Requires `cendor-core >= 1.9` (the ambient seam).

## [1.3.0] — 2026-07-20
Budget identity + a native governance counter, so a monitor can show *which* budget acted and chart block rates. Backward-compatible.

### Added
- **`budget(name=…, description=…)`.** A budget can now carry a human identity that rides every `BudgetEvent` it fires (and is mirrored by `acttrace ≥ 1.7` as `cendor.audit.budget` / `cendor.audit.description`), so an audit stream / monitor shows *which* budget blocked a call — not just that one did. Both are optional; unnamed budgets stay anonymous. Keep `name` a bounded identifier (it is also a counter label).
- **`cendor.tokenguard.budget.events` counter.** Every pre-flight budget action also increments a governance counter on meter `cendor.tokenguard` (no-op without OpenTelemetry), dimensioned by `action`/`model`/`scope`/`name`. Renders as `cendor_tokenguard_budget_events_total` in Prometheus — chart budget-block rates, which a blocked call's absence from the spend counters can't show.

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
