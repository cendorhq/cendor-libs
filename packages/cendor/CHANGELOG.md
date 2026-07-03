# Changelog — cendor (umbrella)

All notable changes to this meta-package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org). This package ships no code; it pins the suite — see each component package's own `CHANGELOG.md` for their changes.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor` — the umbrella meta-package for the Cendor stack. `pip install cendor` pulls the whole suite of composable Python primitives for context, cost, testing, and governance.
- Ships **no code** of its own — it only declares the other packages as dependencies, so they share the `cendor.*` import namespace (PEP 420).
- Pins every member to the 1.x line (`>=1.0.0,<2.0`), so `pip install cendor` always resolves a coherent, tested stack instead of arbitrary latest versions.
- Bundles `cendor-core` (foundation), `cendor-contextkit` (assemble context to a budget), `cendor-squeeze` (reversible compression), `cendor-tokenguard` (cost caps + attribution), `cendor-cassette` (record/replay agent runs), and `cendor-acttrace` (tamper-evident audit log).
- Prefer installing only what you need — each package works standalone and pulls `cendor-core` transitively.
