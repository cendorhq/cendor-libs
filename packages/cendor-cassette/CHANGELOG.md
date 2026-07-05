# Changelog — cendor-cassette

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

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
