# cendor-squeeze

Shrink verbose context — JSON, logs, code, prose — without throwing anything away. Compression
returns a *handle*; the original is always restorable. Content-aware: each type gets a
purpose-built, **deterministic** compressor — **no LLM, no model download, byte-reproducible**.

**Up to ~99% smaller on repetition-heavy logs (~50% on structured data), 100% reversible — zero dependencies, deterministic output.**

![PyPI](https://img.shields.io/pypi/v/cendor-squeeze) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-squeeze`

Using an AI coding assistant? `npx @cendor/init` (TS) / `uvx cendor-init` (Python) wires it up — or point it at [cendor.ai/docs/for-ai-assistants](https://cendor.ai/docs/for-ai-assistants).

```python
from cendor.squeeze import compress

small, handle = compress(huge_json, kind="auto")                 # detect + route
small, handle = compress(source_code, kind="code", fidelity="aggressive")
small, handle = compress(logs, kind="logs", target_tokens=400)   # compress to a budget
original = handle.expand()                                        # restore, byte-for-byte
```

## Highlights

- **Four purpose-built compressors** — JSON (minify + drop nulls; budget-shrink drops keys/elements structurally, staying valid JSON), logs (normalize timestamps/UUIDs/IPs/hex/integers + dedup repeats into `(×N)`, chronological), code (strip comments — *string-aware*, so a `//` or `#` inside a literal stays put; keeps preprocessor & shebang), prose (extractive, abbreviation-aware sentence splitting). `detect()` auto-routes.
- **Compress to a budget** — `target_tokens` is **never exceeded**; `fidelity="lossless" | "balanced" | "aggressive"`. No LLM, no download, byte-reproducible.
- **100% reversible** — a content-addressed store (deduped by hash) keeps every original; `handle.expand()` restores it byte-for-byte no matter how hard you squeeze.
- **Survives restarts** — `handle.to_dict()` / `Handle.from_dict()` next to a durable `squeeze.store.SQLiteStore`; or a bounded `MemoryStore(max_items=…)` via `use_store(...)`.
- **The deterministic default, swappable** — wired through core's `Compressor` protocol (not a hard import), so `contextkit.use_compressor(...)` can replace it globally with any backend (even a heavier ML compressor). squeeze stays the pick for reproducible, offline, audit-friendly output.

**Inbound** — usually `contextkit` calls it for you (`Block(evict="compress")`). Ratios are content-dependent (logs compress most, structured JSON least) — see the [benchmarks](https://github.com/cendorhq/cendor-libs/blob/main/docs/benchmarks.md).

See [`docs/squeeze.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/squeeze.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-squeeze/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
