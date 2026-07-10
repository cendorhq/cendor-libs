# Changelog — cendor-squeeze

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

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
