# Changelog — cendor-contextkit

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.0.2] — 2026-07-10
Deep-QA fix.

### Fixed
- **`Block(messages=[])` no longer reports a misleading "dropped all 0 turns (no room)".** An empty history block is recorded as `kept` with no note, even with a large budget.

## [1.0.1] — 2026-07-05
### Changed
- Repository moved to `github.com/cendorhq/cendor-libs`; `[project.urls]` updated. No API or behavior change.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-contextkit` — treat the context window like a packed suitcase, not a string you concatenate: declare prioritized, pinnable blocks with eviction rules, pack them to a token budget, and get a receipt of exactly what was kept, shrunk, and dropped.
- **Token-budgeted packing** — declare `Block`s with `priority` and `pin`; `assemble()` fits them into the budget deterministically. Pinned blocks are never evicted (raises `BudgetError` if they alone overflow).
- **Per-block eviction** — `drop_oldest` · `truncate` (keep head/tail with a `…[truncated]` marker) · `summarize` (sync, or async via `aassemble()`) · `compress` (via squeeze — **reversible**: the receipt's `BlockDecision.handle.expand()` restores the original, sized against *your* model) · or any custom `EvictionStrategy`.
- **Real chat-history** — `Block(messages=[…])` peels the *oldest turns* to fit (a sliding window) without mangling a turn.
- **An honest receipt** — `report().used == core.tokens.count(assemble(), model)`: packing charges the per-message framing providers add, so a "full" prompt never quietly overflows once sent.
- **Attention-aware ordering** — `order="default"` · `"attention"` (lost-in-the-middle) · `"cache"` (stable prefix for prompt-cache hits).
- **Provider adapters & multimodal** — `for_anthropic()` / `for_gemini()` / `for_bedrock()`; per-image `image_tokens` (int or resolution-aware callable); `whatif(budget)` previews a tighter budget without committing; `use_compressor()` swaps the compression backend.
- **Composes without coupling** — `report()` flows onto core's bus so `acttrace` records what the model actually saw; the optional `contextkit[squeeze]` extra wires in reversible compression via core's `Compressor` protocol.
