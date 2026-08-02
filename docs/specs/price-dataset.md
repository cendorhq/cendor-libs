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
  "_updated": "2026-06-26",// string, ISO date (YYYY-MM-DD) — the table's as-of date (staleness signal)
  "models": {
    "gpt-4o":            { "input": 0.0000025,  "output": 0.00001,   "cached": 0.00000125 },
    "claude-opus-4-8":   { "input": 0.000005,   "output": 0.000025,  "cached": 0.0000005, "cache_write": 0.00000625 },
    "gemini-2.0-flash":  { "input": 0.0000001,  "output": 0.0000004 },
    "text-embedding-3-small": { "input": 0.00000002, "output": 0.0 }
  },
  "_provenance": {         // OPTIONAL, additive — see below. A reader may ignore it entirely.
    "gpt-4o": { "src": "azure", "asof": "2026-07-01" }
  },
  "_units": {              // OPTIONAL, additive — the unit each rate was converted FROM.
    "gpt-4o": { "input": "1M", "output": "1M" }
  },
  "_withheld": [           // OPTIONAL, additive — rows a source published that this table omits.
    { "model": "microsoft/phi-4-mini-instruct", "key": "input", "value": "0.008",
      "ratio": 100000, "cohort": 2, "reason": "phi-4-mini-instruct is better corroborated and is kept" }
  ]
}
```

- **`models`** maps a model id (string) to a **rate object**.
- Rate object fields, all **USD per single token**:
  - `input` — **required**, and greater than zero. Price per input (prompt) token.
  - `output` — **required**. Price per output token. A model that genuinely bills nothing for output
    states `"output": 0`; **omitting the key means the rate is unknown, not zero.**
  - `cached` — optional. Price per cache-**read** token (a subset of input tokens). If absent, cache
    reads fall back to the `input` rate (no discount).
  - `cache_write` — optional. Price per cache-**write** token (a separate category). If absent, it
    defaults to `1.25 × input`.
- ⚠️ **A rate object that cannot price a call MUST NOT be used to price one.** A reader that meets a
  missing `input`, a zero `input`, or a missing `output` **MUST** refuse — the same honest error an
  *unknown model* already gets — rather than substituting a zero. `cached` and `cache_write` are
  different: their fallbacks are *stated above*, so their absence is a documented default, not a gap.
- **Unit is per token**, not per 1K or 1M. e.g. `gpt-4o` `input: 0.0000025` = **$2.50 per 1M tokens**.
- **No schema-version field is required.** `_updated` is the as-of date; the *format* version is this
  spec (`prices/1`), pinned out of band. A table MAY carry `"_schema": "prices/1"` for readers that
  want it; implementations MUST NOT require it.
- ⚠️ **A zero `input` rate is not publishable.** It is indistinguishable from "we do not know", and a
  consumer cannot tell the difference: `estimate()` returns `$0.00` as a *fact* and a USD budget cap
  silently never binds on that model. A model with no known input price MUST be **absent** — the
  honest `None` + warning path — not present at zero. A zero `output` rate is fine and real:
  embeddings have one. (Applied to the reference snapshot 2026-08-02; `llama3` at `0.0/0.0` was the
  row that made exactly one local model report a fabricated `$0.00` while every other reported
  `None`. To price a local model at zero, the *user* says so with `prices.register`.)
- **Unknown top-level keys MUST be ignored.** That is what makes `_provenance` additive.

### Changed 2026-08-02 — an absent rate is *unknown*, not zero

This spec previously read an absent `output` as `0`. That is correct for an embedding, which really
does bill nothing for output, and **wrong for a chat model whose output rate merely failed to
parse** — and downstream the two are indistinguishable, so `estimate()` reported a fabricated
`$0.00` as a *fact* and a USD budget cap under-counted by the whole output side.

Measured on `cendor-core` 1.19.2 / `@cendor/core` 3.6.2, through a documented API:
`refresh(source="litellm")` supplied **10** rows with no output rate, and
`estimate("gpt-image-1", 1_000_000, 1_000_000)` answered **$5.00** where OpenAI's own published
rates ($5/1M text in, $40/1M image out) make it **$45.00**. `refresh(source="azure")` supplied one
more (`fw-deepseek-v4-pro-ch`).

The rule is now symmetric with the `input` bullet above, which this spec has always had. It is not a
new idea, only the same one applied to the field next door.

**What this changes for a table author.** State `output` — as a real rate, or as an explicit `0`
when the model genuinely has no output billing. Both reference implementations refuse an unpriceable
row rather than guessing, and the error they raise names the fix:

<!-- tabs: lang -->
<!-- tab: Python -->
```python
prices.register_model_price("my-model", input=5.00, output=40.00, per="1M")
prices.register("my-model", {"input": "0.000005", "output": 0})   # per-token; 0 = no output billing
```
<!-- tab: TypeScript -->
```ts
prices.registerModelPrice('my-model', { input: 5.0, output: 40.0, per: '1M' });
prices.register('my-model', { input: '0.000005', output: 0 }); // per-token; 0 = no output billing
```
<!-- /tabs -->

**Why the version string is unchanged.** The document *shape* is identical — same keys, same types,
same optionality — and the only movement is that a reader now refuses where it used to guess, which
is strictly more conservative. Nothing that reads a conformant table behaves differently. Minting
`prices/2` would have rippled `_schema` through the feed and both bundled snapshots and required a
stated `prices/1` ⇄ `prices/2` compatibility rule, for a change no conformant table can notice.

**A rate the user registers is never second-guessed.** `prices.register("llama3", {"input": 0,
"output": 0})` still prices a local model at zero: the spec already says a user registration
outranks any table, and a zero a *person* wrote is a statement, while a zero that arrived inside a
fetched table is a parser having lost one.

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
       + output_rate × output_tokens                        // rate.output; ABSENT -> refuse, see below
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
- **Unpriceable rates → the same error** (`MissingRateError`, a *subclass* of `UnknownModelError` so
  every existing handler is unaffected), raised whenever the model is priced — not only when the
  call happens to carry output tokens. A table that cannot price a model cannot price it, and
  learning that on the first output-bearing call rather than the first call is a late, partial
  signal. The three unpriceable shapes are: no `input`, a **table-stated** zero `input`, and no
  `output`. A rate the *user* registered is exempt from the zero rule — see the 2026-08-02 note.
- **Lookup normalization.** The table keys are bare ids. When the exact id misses the table, the
  implementation retries once with a normalized key before erroring: lowercase; drop a
  `provider/`-style prefix; drop leading **alpha-only** dotted segments (Bedrock vendor/region
  namespaces — `anthropic.`, `us.anthropic.` — never in-name dots like `gpt-4.1`); drop a trailing
  Bedrock version (`-v1:0`, `-v2`); drop a trailing date (`-20260115` or `-2025-11-13`). So
  `us.anthropic.claude-sonnet-4-6-20260115-v1:0` prices like `claude-sonnet-4-6`. Normalization
  never invents a price — a decorated unknown still raises.
- The token subset conventions match the [`Usage` event shape](bus-events.md): `cached_tokens ⊆ input`,
  `reasoning_tokens ⊆ output` (reasoning is already billed inside output, so it is **not** a separate
  term here).

## `_provenance` (optional, additive)

A table MAY carry a top-level `_provenance` object mapping the **same model ids** as `models` to
`{ "src": string, "asof": "YYYY-MM-DD" | null }` — which source that specific rate came from, and
that source's own as-of date (never the day it was fetched).

It is a **parallel map, deliberately not a field inside the rate object**. Rate objects stay pure
numbers, so a `prices/1` reader that walks `models[id]` and multiplies never meets a string, and no
provenance value can reach a decimal coercion. A reader that ignores unknown top-level keys — which
this spec requires — is unaffected, which is what makes the key additive rather than a version bump.

`asof` MAY be `null`: some sources publish no date at all, and a table MUST NOT invent one. An
undatable rate is undatable, not fresh.

## `_units` and `_withheld` (optional, additive)

Both are **producer-side** keys. A consumer never needs them to price a call, and a `prices/1` reader
ignores them like any other unknown top-level key. They exist so that a *table built from other
tables* can be audited.

`_units` maps a model id to `{ <rate key>: "token" | "1K" | "1M" }` — the unit the published
per-token rate was **converted from**. It is not decoration. A vendor that moves a meter from `1K` to
`1M` and changes the price in the same run lands on a number that no value-based check can question:
the swing looks ordinary, the absolute value looks plausible, and the rate is off by 1000. Comparing
the recorded unit against the previous table's is the only signal that survives that, so the unit
travels with the rate that won.

`_withheld` lists rows a source published that this table deliberately does **not** carry, each with
`model`, `key`, the rejected `value`, and a `reason`. Producers reconciling several catalogs will
sometimes find two irreconcilable prices for one model; dropping the doubtful row is right, but
dropping it *silently* makes a suppressed model and an unknown model look identical, which overstates
coverage. Stating the omission costs a few lines and keeps the table honest about its own gaps.

Neither key is required, and a table carrying neither is fully conformant — a hand-authored snapshot
has no upstream to reconcile.

## Refresh sources (informative)

`refresh()` can replace the table at runtime from public, no-auth sources — each normalized back into
the `{ "models": { … } }` schema above:

| Name | What | Dated |
|---|---|---|
| *(default)* | the **cendor-prices feed** (`cendorhq.github.io/cendor-prices/prices.json`) — the sources below, reconciled, with `_provenance` | yes |
| `azure` | Microsoft Azure Retail Prices, Foundry Models meters, one region | yes |
| `aws` | AWS Bedrock public price files, one region, both offer codes | yes |
| `modelsdev` | models.dev (MIT) | yes, per row |
| `litellm` | LiteLLM (MIT) | no |
| `openrouter` | OpenRouter — gateway **resale** prices | no |
| `vercel` | Vercel AI Gateway — gateway **resale** prices | no |

An implementation MUST NOT stamp `_updated` with "today" for a source that publishes no date: a
faked date defeats the staleness signal the field exists to provide.

A provider- or gateway-reported cost is always preferred over an estimate when available (labeled
`cost_reported` vs `cost_estimated`); this dataset is the fallback estimator, and its shape is the
contract. A **user registration** outranks any table and survives every refresh.
