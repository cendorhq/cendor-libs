# Changelog — cendor-contextkit

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.1.0] — 2026-07-31

### Added
- **`Context(on_missing_compressor="note" | "warn" | "error")`** — how loud it is when a block asks
  for `evict="compress"` and no compressor is available. That block is **truncated** instead, and
  truncation is a different operation, not a slightly worse one: it discards content and gives you no
  `Handle` to `.expand()`. The substitution has always been recorded as a note on the block's
  `BlockDecision`, but a note lives inside the `AssemblyReport` and nothing obliges a caller to read
  one — so a forgotten `cendor-contextkit[squeeze]` extra quietly degraded every compress block while
  the assembly still reported success. `"warn"` adds a `MissingCompressorWarning`; `"error"` raises
  `MissingCompressorError` naming every way out. **The default is `"note"`, i.e. unchanged** — no
  existing assembly behaves differently. It fires only when the compressor is genuinely missing: a
  block that asked for `truncate`, or one that fitted the budget and was never evicted, is untouched
  in every mode.

## [1.0.3] — 2026-07-11
AI-assistant onboarding: inline Type Teach ships inside the package, plus the bundled integration guide. No runtime behavior change for correct code.

### Added
- Inline `@example` + a correct-shape signature on public symbols, so your editor's language server (and agent-mode assistants) is handed the right call as you type — the wrong shape becomes a type error whose message states the right one.
- `INTEGRATION.md` is now bundled in the installed package — a one-screen "call Cendor correctly" guide. Full trap sheet: https://cendor.ai/docs/for-ai-assistants

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
