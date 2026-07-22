# Changelog — cendor-cassette

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.1.0] — 2026-07-22
Record/replay key off a session id stamped at call initiation, so an out-of-scope streamed call is captured correctly.

### Fixed
- Record / replay now key off a session id stamped **at call initiation** (via the `cendor-core` ambient seam), with the delivery-time contextvar kept only as a split-brain fallback. A streamed call created inside a `using()` block but drained on a detached consumer (or while a different session's scope is active) is now recorded into — and replayed from — the correct cassette, instead of being lost or captured by the wrong session. The session id is a reserved top-level metadata key, excluded from the replay fingerprint, so **every existing recorded cassette replays byte-identically** — nothing to re-record.

### Changed
- Requires `cendor-core >= 1.9` (the ambient seam).

## [1.0.2] — 2026-07-11
AI-assistant onboarding: inline Type Teach ships inside the package, plus the bundled integration guide. No runtime behavior change for correct code.

### Added
- Inline `@example` + a correct-shape signature on public symbols, so your editor's language server (and agent-mode assistants) is handed the right call as you type — the wrong shape becomes a type error whose message states the right one.
- `INTEGRATION.md` is now bundled in the installed package — a one-screen "call Cendor correctly" guide. Full trap sheet: https://cendor.ai/docs/for-ai-assistants

## [1.0.1] — 2026-07-05
### Changed
- Repository moved to `github.com/cendorhq/cendor-libs`; `[project.urls]` updated. No API or behavior change.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-cassette` — record an agent run once, replay it forever: deterministic, offline, and free. Unlike `vcrpy` (HTTP-only), it captures the *whole* run — every LLM call and tool call, in order — via core's bus and interceptor, with no second patch and no network.
- **Whole-run capture** — every LLM **and** tool call, in order.
- **Four modes** — `auto` (record then replay) · `record` · `replay` (fail on an unrecorded call) · `rerecord` (run live and report `drift()` without overwriting the committed cassette).
- **Decorator or context manager** — `@cassette.use("run.json")` / `with cassette.using(...)` (handy in pytest fixtures).
- **Meaning-based assertions** — `semantic_match(actual, expected)` with an offline lexical default and opt-in scorers: a free offline local-embedding scorer (model2vec, `cendor-cassette[embeddings]`), a BYO-provider embedder, or an LLM judge. `semantic_drift()` filters `rerecord` noise down to real regressions.
- **Pluggable matching + redaction** — a `normalizer` ignores volatile fields, and secrets/PII are redacted on write, but matching hashes the **un-redacted** request so redaction never collapses two distinct calls (`redact=True|False|callable`).
- **Parallel-safe** — recording is scoped to the active `using()` / `use()` context (a `ContextVar`) and cassettes are written atomically; under pytest-xdist, give each worker its own cassette path.
- **Faithful replay** — dict-response providers (Ollama/Bedrock) replay as dicts and SDK-object providers as attribute objects; `stream=True` / `stream=False` calls match their own recordings (cassette format **v2**; committed v1 cassettes still replay).
- **`promote()`** turns a production JSONL trace into a replayable regression test (LLM **and** tool calls).
