# Cendor

The umbrella meta-package for the **Cendor** stack — *production plumbing for LLM applications*.
Installing it pulls the whole suite of composable Python primitives for context, cost,
testing, and governance.

**One install, the entire stack — `pip install cendor`.**

![PyPI](https://img.shields.io/pypi/v/cendor) ![license](https://img.shields.io/badge/license-Apache_2.0-blue)

This package ships **no code** of its own — it only declares the others as dependencies, so they
share the `cendor.*` import namespace:

| | Import | Role |
|---|---|---|
| `cendor-core` | `cendor.core` | foundation (types · tokens · prices · `instrument` · bus · OTel) |
| `cendor-contextkit` | `cendor.contextkit` | assemble context within a budget |
| `cendor-squeeze` | `cendor.squeeze` | reversible, content-aware compression |
| `cendor-tokenguard` | `cendor.tokenguard` | pre-flight cost caps + attribution |
| `cendor-cassette` | `cendor.cassette` | record/replay agent runs |
| `cendor-acttrace` | `cendor.acttrace` | tamper-evident audit log |

Prefer to install only what you need — each package works standalone and pulls `cendor-core`
transitively. See the [project README](https://github.com/cendorhq/Cendor) for the
composition story and examples.

*Powered by [PowerAI Labs](https://powerailabs.dev). Apache-2.0 licensed; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
