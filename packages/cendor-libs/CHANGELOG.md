# Changelog — cendor-libs (umbrella)

All notable changes to this meta-package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org). This package ships no code; it pins the suite — see each component package's own `CHANGELOG.md` for their changes.

## [1.2.0] — 2026-07-09
### Changed
- Raised member floors so a fresh install always resolves the current capabilities:
  `cendor-acttrace>=1.4.0` (guardrail-decision chaining) and `cendor-guardrails>=1.2.0`
  (hosted rails, config-as-data, grounding). All members stay capped below 2.0.

## [1.1.0] — 2026-07-09
### Added
- `cendor-guardrails` joins the umbrella — the seventh library (the deterministic gate).
  `pip install cendor-libs` now pulls all seven: core, tokenguard, contextkit, squeeze,
  guardrails, cassette, acttrace.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-libs` — the umbrella meta-package for the Cendor stack. `pip install cendor-libs` pulls the whole suite of composable Python primitives for context, cost, testing, and governance. (This is the canonical umbrella name; the brand alias `cendor` depends solely on it.)
- Ships **no code** of its own — it only declares the other packages as dependencies, so they share the `cendor.*` import namespace (PEP 420).
- Pins every member to the 1.x line (`>=1.0.0,<2.0`), so `pip install cendor-libs` always resolves a coherent, tested stack instead of arbitrary latest versions.
- Bundles `cendor-core` (foundation), `cendor-contextkit` (assemble context to a budget), `cendor-squeeze` (reversible compression), `cendor-tokenguard` (cost caps + attribution), `cendor-cassette` (record/replay agent runs), and `cendor-acttrace` (tamper-evident audit log).
- Prefer installing only what you need — each package works standalone and pulls `cendor-core` transitively.
