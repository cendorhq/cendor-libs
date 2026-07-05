# Price dataset

**Spec version:** `prices/1` · **Status:** stable · **Implemented by:** `cendor-core` (`cendor.core.prices`)

One model price table, consumed the same way in every language, producing **exact decimal** costs —
never binary floats. This spec pins the dataset shape and the cost formula so a JS/TS port bills
identically to Python from the same data.

## Dataset shape

A JSON object. The bundled snapshot lives at `cendor-core`'s `prices.json`; a refreshed table has the
same shape.

```jsonc
{
  "_note": "…",            // string, informational only
  "_updated": "2026-06-26",// string, ISO date (YYYY-MM-DD) — the snapshot's as-of date (staleness signal)
  "models": {
    "gpt-4o":            { "input": 0.0000025,  "output": 0.00001,   "cached": 0.00000125 },
    "claude-opus-4-8":   { "input": 0.000005,   "output": 0.000025,  "cached": 0.0000005, "cache_write": 0.00000625 },
    "gemini-2.0-flash":  { "input": 0.0000001,  "output": 0.0000004 },
    "llama3":            { "input": 0.0,        "output": 0.0 }
  }
}
```

- **`models`** maps a model id (string) to a **rate object**.
- Rate object fields, all **USD per single token**:
  - `input` — required. Price per input (prompt) token.
  - `output` — price per output token (treated as `0` if absent).
  - `cached` — optional. Price per cache-**read** token (a subset of input tokens). If absent, cache
    reads fall back to the `input` rate (no discount).
  - `cache_write` — optional. Price per cache-**write** token (a separate category). If absent, it
    defaults to `1.25 × input`.
- **Unit is per token**, not per 1K or 1M. e.g. `gpt-4o` `input: 0.0000025` = **$2.50 per 1M tokens**.
- **No schema-version field.** `_updated` is the as-of date; the *format* version is this spec
  (`prices/1`), pinned out of band.

## Decimal rule (mandatory in every language)

Rates are money. The reference parses the JSON decoding every number as an arbitrary-precision
**decimal** (`json.loads(text, parse_float=Decimal)`) — never IEEE binary float. A conforming port
**must** do the same (parse rates with a decimal reviver / decimal library), or costs will drift from
Python's in the last digits. Authored snapshots may use JSON number literals as above; implementations
must not let them round-trip through `float`/`double`.

## Cost formula

`estimate(model, input_tokens, output_tokens=0, cached_tokens=0, cache_write_tokens=0) -> Money` computes,
with all arithmetic in decimal:

```
cached      = clamp(cached_tokens, 0, input_tokens)         // cached ⊆ input
input_rate  = rate.input
cached_rate = rate.cached      if present else input_rate
write_rate  = rate.cache_write if present else input_rate × 1.25

amount = input_rate  × (input_tokens − cached)
       + output_rate × output_tokens                        // output_rate = rate.output or 0
       + cached_rate × cached
       + write_rate  × max(cache_write_tokens, 0)

cost = Money(amount, "USD")
```

Key points a port must replicate exactly:

- **Cached tokens are billed once.** They are a subset of `input_tokens`; the non-cached remainder is
  billed at `input_rate` and the cached portion at `cached_rate`. When there's no `cached` rate this
  reduces to `input_rate × input_tokens` — never a double charge.
- **`cache_write` is a separate, added category** (not part of `input_tokens`), defaulting to
  `1.25 × input` when unpriced.
- **Unknown model → error** (`UnknownModelError`), not a silent `0`. (Callers that must not fail on
  unpriced models handle that at a higher layer — see `tokenguard`'s `on_unpriced`.)
- The token subset conventions match the [`Usage` event shape](bus-events.md): `cached_tokens ⊆ input`,
  `reasoning_tokens ⊆ output` (reasoning is already billed inside output, so it is **not** a separate
  term here).

## Refresh sources (informative)

`refresh()` can replace the table at runtime from public, no-auth sources — the bundled snapshot URL
(`raw.githubusercontent.com/cendorhq/cendor-libs/main/…/prices.json`), LiteLLM, OpenRouter, or Azure
retail prices — each normalized back into the `{ "models": { … } }` schema above. A provider- or
gateway-reported cost is always preferred over an estimate when available (labeled `cost_reported` vs
`cost_estimated`); this dataset is the fallback estimator, and its shape is the contract.
