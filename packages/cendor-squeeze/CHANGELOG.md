# Changelog — cendor-squeeze

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.1.2] — 2026-08-01
`compress()` stops paying for an event nobody is listening to. No API change; no event-shape change.

### Fixed
- **The `CompressionEvent` is now computed only when the bus has a subscriber.** Since 1.1.0 every
  `compress()` ran `tokens.count()` twice — over the original *and* the compressed text — to fill
  the metadata-only event **before** `bus.emit`, whether or not anything was subscribed. Measured on
  a 90.1 KB JSON payload with zero subscribers: 20.29 ms/call with the event vs 1.42 ms without —
  **93% of the call**, and tokenizing is linear in payload size, so every large compress paid it
  (including `contextkit`'s `evict="compress"` path, per block). `_emit_compression` now returns
  before any counting when `bus.has_subscribers()` (new in `cendor-core` 1.18.0) is false. An event
  with no subscriber is unobservable, so nothing observable changes; with anything attached — an
  acttrace `AuditLog`, a monitor exporter — the event is emitted exactly as before, same fields,
  same counts, same duck-typed `compression` audit entry. Honest limit: the check is "is anyone on
  the bus", so an app with, say, tokenguard armed still computes the counts — that is the cost of
  visibility, now paid only when something can see it.

### Changed
- `cendor-core` floor raised to `>=1.18` (for `bus.has_subscribers()`).

## [1.1.1] — 2026-07-24
Package-level store exports. Backward-compatible.

### Added
- `MemoryStore` and `SQLiteStore` are now exported at the package top level (`from cendor.squeeze import SQLiteStore`) and listed in `__all__`, matching the TypeScript port's index exports. They remain available under the `cendor.squeeze.store` submodule as before, so no existing import breaks.

## [1.1.0] — 2026-07-20
Compression visibility on the bus (G21) — squeeze stops being dark to a monitor/audit.

### Added
- **`CompressionEvent`** — a metadata-only bus event emitted after each `compress()`: `technique`, `tokens_before`, `tokens_after`, `ratio` (tokens remaining), `store_kind`, `handle_id`, `kind`, `trace_id`, `ts`. It carries **only the shape** of a compression — never the text — so a monitor or the acttrace audit can show squeeze activity and token savings without any content leaving the process. `acttrace` (≥ 1.8) duck-types it into a `compression` audit entry + an `audit.compression` span.

## [1.0.3] — 2026-07-11
AI-assistant onboarding: inline Type Teach ships inside the package, plus the bundled integration guide. No runtime behavior change for correct code.

### Added
- Inline `@example` + a correct-shape signature on public symbols, so your editor's language server (and agent-mode assistants) is handed the right call as you type — the wrong shape becomes a type error whose message states the right one.
- `INTEGRATION.md` is now bundled in the installed package — a one-screen "call Cendor correctly" guide. Full trap sheet: https://cendor.ai/docs/for-ai-assistants

## [1.0.2] — 2026-07-10
Deep-QA fixes.

### Fixed
- **Budgeted JSON compression no longer collapses a wrapped payload to `{}`.** The fitter now recurses into a payload nested under a single key (`{"data":[…]}`, `{"results":{…}}`), peeling elements / keys largest-first, instead of dropping the whole key — so `contextkit`'s `Block(evict="compress")` keeps real content under a budget. Output stays valid JSON and `expand()` is still byte-exact.
- **A non-JSON-serializable input** (e.g. a `set`) now raises a clear `squeeze`-friendly `TypeError` instead of a raw stdlib error.

### Docs
- Code-compression benchmark re-measured on a representative module (**~17%**, replacing the comment-heavy ~53% headline); documented that string literals and docstrings are preserved (so comment-sparse code compresses little, and `aggressive` ≈ `balanced` there), and that `expand()` returns canonical JSON for object inputs.

## [1.0.1] — 2026-07-05
### Changed
- Repository moved to `github.com/cendorhq/cendor-libs`; `[project.urls]` updated. No API or behavior change.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-squeeze` — shrink verbose context (JSON, logs, code, prose) without throwing anything away: `compress()` returns a *handle* and `expand()` restores the original byte-for-byte. Content-aware, deterministic, no LLM and no model download.
- **Four purpose-built compressors** — JSON (minify + drop nulls; budget-shrink drops keys/elements structurally, staying valid JSON), logs (normalize timestamps/UUIDs/IPs/hex/integers + dedup repeats into `(×N)`, chronological), code (string-aware comment stripping that keeps preprocessor & shebang lines), and prose (extractive, abbreviation-aware sentence splitting). `detect()` auto-routes; `kind=` overrides.
- **Compress to a budget** — `target_tokens` is **never exceeded**; `fidelity="lossless" | "balanced" | "aggressive"` trades structure for size.
- **100% reversible** — a content-addressed store (deduped by hash) keeps every original, so `handle.expand()` restores it byte-for-byte no matter how hard you squeeze.
- **Survives restarts** — `handle.to_dict()` / `Handle.from_dict()` alongside a durable `squeeze.store.SQLiteStore`, or a bounded `MemoryStore(max_items=…)` via `use_store(...)`.
- **The deterministic default, swappable** — wired through core's `Compressor` protocol (not a hard import), so `contextkit.use_compressor(...)` can replace it globally with any backend while squeeze stays the pick for reproducible, offline, audit-friendly output.
