# Changelog — cendor (brand alias)

All notable changes to this meta-package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org). This package ships no code; it is a thin alias for `cendor-libs`.

## [1.1.0] — 2026-07-05
### Changed
- `cendor` is now a **brand alias** for the stack: its sole dependency is `cendor-libs>=1.0,<2.0` (the canonical umbrella meta-package). `pip install cendor` and `pip install cendor-libs` resolve to the same suite.
- Prior to 1.1.0, `cendor` was itself the umbrella that pinned the six libraries directly; that role moved to `cendor-libs`. The change is transparent — installing `cendor` still pulls the whole stack — and there were no external users at the time of the change.
- Ships **no code** of its own; the six libraries share the `cendor.*` import namespace (PEP 420) exactly as before.
