# cendor

Brand alias for the **Cendor** stack. `pip install cendor` pulls the whole suite of composable
Python primitives for context, cost, testing, and governance — it does this by depending solely on
[`cendor-libs`](https://pypi.org/project/cendor-libs/), the canonical umbrella meta-package.

**Prefer `pip install cendor-libs`** — that's the documented name. `cendor` exists so the brand name
keeps working forever; both resolve to the same stack.

![PyPI](https://img.shields.io/pypi/v/cendor) ![license](https://img.shields.io/badge/license-Apache_2.0-blue)

This package ships **no code** of its own. Installing it is exactly equivalent to installing
`cendor-libs`, which declares the six libraries as dependencies so they share the `cendor.*` import
namespace (PEP 420):

| | Import | Role |
|---|---|---|
| `cendor-core` | `cendor.core` | foundation (types · tokens · prices · `instrument` · bus · OTel) |
| `cendor-contextkit` | `cendor.contextkit` | assemble context within a budget |
| `cendor-squeeze` | `cendor.squeeze` | reversible, content-aware compression |
| `cendor-tokenguard` | `cendor.tokenguard` | pre-flight cost caps + attribution |
| `cendor-cassette` | `cendor.cassette` | record/replay agent runs |
| `cendor-acttrace` | `cendor.acttrace` | tamper-evident audit log |

Looking for the governed agent SDK? See [`cendor-sdk`](https://pypi.org/project/cendor-sdk/)
(imports as `cendor.sdk`, same namespace). See the
[project README](https://github.com/cendorhq/cendor-libs) for the composition story and examples.

*Powered by [PowerAI Labs](https://powerailabs.dev). Apache-2.0 licensed; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
