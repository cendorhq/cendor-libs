# Testing plan — cendor

How this monorepo is tested, across five layers. The goal: prove correctness at increasing levels
of realism — from fast offline unit tests, through the *shipped artifact*, to *real provider
response shapes* frozen as offline fixtures. Conventions for writing tests live in the
[`write-tests` skill](.claude/skills/write-tests/SKILL.md); this document is the overall strategy,
status, and how-to-run.

## Principles (non-negotiable)
- **No network in any automated test.** Provider clients are mocked; golden values are pinned.
- **Deterministic & fast.** Each test < 1s; token counts forced onto the offline heuristic where
  exactness matters; `Money` asserted as `Decimal`.
- **Test the public surface**, not internals; keep the namespace invariant (`namespace-guard`).
- New behavior ships with tests in the same PR (see the per-package checklist below).

## The five layers

| Layer | What it proves | Where | Runs in CI | Status |
|---|---|---|---|---|
| **A. Unit** | logic is correct (mocked, offline) | `packages/*/tests/` | ✅ yes | ✅ in place (430+ tests) |
| **B. Install / import smoke** | the wheels + PEP 420 namespace import for a real user | clean venv (matrix 3.11–3.13) | ✅ yes | ✅ in place (`smoke` job) |
| **C. Cookbook integration** | documented usage runs against installed packages | `cendor-cookbook` (separate public repo) | n/a here | ⏳ in the cookbook |
| **D. Real-provider fixtures** | `instrument()` parses *actual* OpenAI/Anthropic/Bedrock/Gemini/Ollama responses | recorded cassettes | gated | ⏳ planned (record-once → replay) |
| **E. Property / edge** | invariants hold for arbitrary inputs | `packages/*/tests/test_*_properties.py` (hypothesis) | ✅ yes | ✅ in place (9 tests) |

Plus static gates in CI: `ruff check`, `ruff format --check`, **`mypy`** (all seven packages), and the namespace-guard.

### Layer A — Unit tests *(implemented)*
The correctness guard for all logic. Mocked clients, golden values, no network. Run:

```bash
uv run pytest                                   # all packages
uv run pytest packages/cendor-core         # one package
uv run pytest -q --collect-only                 # inventory
```

Plus the static gates:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy -p cendor.core -p cendor.tokenguard -p cendor.contextkit \
            -p cendor.squeeze -p cendor.cassette -p cendor.acttrace -p cendor.guardrails
# namespace invariant — must print nothing:
find packages -path '*/src/cendor/__init__.py' -print
```

**Current coverage matrix (Layer A):**

| Package | Tests | Covers |
|---|---:|---|
| `core` | 124 | `Money`/Decimal arithmetic & `Usage` (incl. cached-token pricing + `cache_write`); golden token counts + family detection + register + native-vs-fallback `method()`; `prices.estimate`/cached/unknown-model/`refresh` (undatable) fallback; `instrument` sync+async+idempotent+unpriced; **Responses API + google-genai** shapes; streaming (all providers); `instrument_tool`; interceptor replay + `Reroute`; `otel.ingest` (+ cached/reasoning); thread-safe bus; protocol shapes |
| `tokenguard` | 54 | `@budget` raise / block / truncate / token cap / callable / nested / **downgrade** / **clamp**; unpriced-model warnings + `on_unpriced`; `max_tokens=0`; `track` + `report(group_by)` + `assert_under` + `unpriced_calls`; streaming-timing; `SQLiteSink`/`OTelSink` + cross-thread; concurrent-record eviction; async |
| `contextkit` | 45 | budgeted `assemble` + every eviction strategy; pinned-overflow `BudgetError`; `report` receipt (+ squeeze `handle`); `whatif`; `order="attention"/"cache"`; adapters (anthropic/gemini/bedrock) + role coercion; multimodal `image_tokens`; async summarizer; compressor model-forwarding; determinism |
| `squeeze` | 32 | `detect`; JSON/logs/prose/**code** compression + exact reversibility; structural JSON fit; length-normalized prose + abbreviation splitting; log IP/hex/int normalization; `fidelity`; `Compressor` protocol; CCR dedup + `SQLiteStore` + **LRU** `MemoryStore`; deterministic `Handle.id`; contextkit↔squeeze |
| `cassette` | 29 | record→replay (LLM + tools); dict-vs-object replay; streaming record→replay (sync+async); `stream` in the hash + v1 compat; version check; ContextVar parallel-safety; redaction (modern secret formats); `promote` (incl. tools); `rerecord` + `drift`; `semantic_match` + pluggable scorer |
| `acttrace` | 152 | auto-population from the bus; hash-chain verify + tamper/reorder detection; forged/stripped/unauthenticated `_meta`; missing/corrupt-file handling; context-assembly + **guardrail-decision** capture (duck-typed); EU AI Act + NIST export; redaction (modern formats) + auto-flag; HMAC signing + `verify(key=)`; concurrent-emit chain integrity; `acttrace verify` CLI |
| `guardrails` | 142 | the `Guardrail`/`Verdict`/`Context` abstraction + `@guardrail` decorator (+ `metadata`) + stage validation; every built-in rule (`keyword_deny`/`regex_rule`/`url_allowlist`/`url_deny`/`length_bounds`/`json_schema`/`custom`); block/redact/flag across the four stages; `apply`/`evaluate` (sync) + `apply_async`/`evaluate_async`; decision emission + context/metadata propagation + ambient trace-id; `install()` interceptor (block pre-spend, redact reroute, pass MISS, tool_call block, output post-flight) + `uninstall` + `scoped()`; per-guardrail `timeout`/`on_error`; `judge` helpers; detection-tier adapters (`classifier`/`prompt_guard`/`language`/`openai_moderation`) + hosted rails (`bedrock_guardrail`/`azure_content_safety`/`model_armor`, mocked clients, no network); `load_policy` config-as-data (hash/version stamped) + `groundedness`/`denied_topics` (BYO embeddings) |

### Layer B — Install / import smoke *(implemented)*
Layer A runs against the *editable source*; this layer builds the **wheels** and installs them into a
clean venv (offline, no PyPI) on Python 3.11/3.12/3.13, then imports every submodule — catching
packaging / namespace / metadata breakage a user would hit. It's the `smoke` job in
[`ci.yml`](.github/workflows/ci.yml). Reproduce locally:

```bash
for p in cendor-core cendor-tokenguard cendor-contextkit \
         cendor-squeeze cendor-cassette cendor-acttrace cendor-guardrails cendor-libs cendor; do uv build --package "$p"; done
python -m venv .smoke && .smoke/bin/pip install --no-index --find-links dist cendor-libs
.smoke/bin/python -c "import cendor.core, cendor.tokenguard, cendor.contextkit, cendor.squeeze, cendor.cassette, cendor.acttrace, cendor.guardrails; print('ok')"
```

### Layer C — Cookbook integration *(in the cookbook repo)*
The public `cendor-cookbook` repo's offline examples (mock client, no keys) double as
end-to-end checks that documented usage works on the installed packages. Lives there, not here, so
this repo stays the clean library source.

### Layer D — Real-provider fixtures *(to add)*
The mocks in Layer A approximate provider responses; only real calls confirm the adapters parse
actual `usage`/shape. The pattern (uses `cassette`):
1. Run a provider example **once with real keys** (small `max_tokens`).
2. `cassette` records the run to `fixtures/<provider>.json`.
3. Commit the fixture; **replay it offline forever** in CI — real-shape coverage, zero per-run cost.

This belongs alongside the cookbook's `providers/*` examples; the recorded cassettes are the
regression suite for `instrument()`'s adapters.

### Layer E — Property / edge tests *(implemented)*
`hypothesis` property tests (`packages/*/tests/test_*_properties.py`) for the deterministic guarantees:
- `squeeze`: `expand(compress(x)) == x` for arbitrary text, every kind (reversibility).
- `contextkit`: assembled tokens ≤ `budget − reserve_output`; assembly is deterministic.
- `core`: `Money` add/sub round-trips exactly; `prices.estimate` is monotonic in tokens.
- `acttrace`: a fresh chain verifies; any single-entry tamper makes `verify` return `False`.

> This layer earned its keep immediately: it surfaced a real bug — `acttrace.verify` used
> `str.splitlines()`, which also breaks on Unicode line separators (U+2028/U+0085) that can appear
> inside a payload. The fix: `acttrace.verify` splits on `\n` only, so such payloads verify correctly.

## CI mapping
[`ci.yml`](.github/workflows/ci.yml) runs on every push/PR:
- **`test` job:** `uv sync` → `ruff check` → `ruff format --check` → namespace-guard → `mypy` →
  `pytest` (Layers A + E + static gates).
- **`smoke` job:** build wheels → install into a clean venv (offline) on Python 3.11/3.12/3.13 →
  import the namespace (Layer B).

**Not yet in CI:** Layer D (real-provider fixtures) — it needs provider secrets and lives with the
cookbook; wire it as a gated/manual job when those fixtures exist.

## When you add or change code
1. Add/expand **Layer A** unit tests in the package's `tests/` (mock clients, golden values, no
   network) — per the `write-tests` skill.
2. If it touches a **provider adapter**, add a mock-shaped test here *and* plan a Layer D fixture.
3. If it's a **deterministic guarantee**, add a Layer E property test.
4. Run the full Layer A gate (pytest + ruff + format + mypy + namespace-guard) before committing,
   and run the **namespace-guard** skill before any release.
