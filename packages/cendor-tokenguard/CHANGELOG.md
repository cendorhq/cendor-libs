# Changelog — cendor-tokenguard

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

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
