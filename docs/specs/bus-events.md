# Bus event shapes

**Spec version:** `events/1` · **Status:** stable · **Implemented by:** `cendor-core` (`cendor.core.types`, bus)

`instrument()` normalizes every provider call into two canonical events — `LLMCall` and `ToolCall` —
carrying two value types, `Usage` and `Money`. These four shapes are the **cross-language vocabulary**:
every tool subscribes to the same events, and every language implementation emits the same field names
and conventions. This spec pins them so behavior and serialization match across ports.

## Value types

### `Money`

The only representation of money anywhere in the stack.

| field | type | notes |
|---|---|---|
| `amount` | decimal | arbitrary-precision decimal — **never** an IEEE float. Constructed from int/str/decimal; a float input is coerced via its string form to avoid binary noise. |
| `currency` | string | default `"USD"`. |

- Arithmetic and comparison require **matching currency** (mismatched currencies are an error).
- **Scalar serialization** (where a payload stores Money as one value, e.g. an audit entry): the string
  `"{amount} {currency}"`, e.g. `"0.0025 USD"`. A port must serialize/parse this exact form.
- A port must back `amount` with a decimal type (decimal.js / big.js / equivalent), not `number`.

### `Usage`

Token usage for one LLM call. Subset conventions matter for correct pricing.

| field | type | default | meaning |
|---|---|---|---|
| `input_tokens` | int | — | billed input (prompt) tokens, **inclusive of** `cached_tokens`. |
| `output_tokens` | int | `0` | billed output tokens, **inclusive of** `reasoning_tokens`. |
| `cached_tokens` | int | `0` | cache-**read** tokens — a **subset of** `input_tokens`. |
| `reasoning_tokens` | int | `0` | reasoning/thinking tokens — a **subset of** `output_tokens`. `0` for providers that don't report it separately. |
| `cache_write` | int | `0` | cache-**write** tokens — a **separate** billed category, *not* part of `input_tokens`. |
| `total_tokens` | int (derived) | — | `input_tokens + output_tokens`. Cached/reasoning are subsets (not added); `cache_write` is billed separately (not added). |

Normalization is the implementation's job **before** emitting: e.g. Anthropic reports input excluding
cache reads, so extraction folds `cache_read_input_tokens` back into `input_tokens` so the subset
convention holds for every provider. A port must normalize to these same conventions, or pricing
([Price dataset](price-dataset.md)) double-counts or under-counts.

## Events

Both events are emitted on the in-process bus (`subscribe`/`emit`); subscribers receive the event object.
Fields marked *runtime* are process-local and are generally **not** persisted (see the cassette /
acttrace specs for exactly what each persists).

### `LLMCall`

| field | type | default | notes |
|---|---|---|---|
| `id` | string | — | call id (runtime). |
| `provider` | string | — | e.g. `"openai"`, `"anthropic"`. Part of the identity of a call. |
| `model` | string | — | model id. |
| `messages` | array of objects | — | provider-native message dicts (not further normalized). |
| `usage` | `Usage` \| null | `null` | populated after the call. |
| `cost` | `Money` \| null | `null` | populated after pricing. |
| `latency_ms` | number \| null | `null` | runtime. |
| `trace_id` | string | `""` | correlation id set by `trace()`; `""` = uncorrelated. |
| `ts` | timestamp \| null | `null` | runtime. |
| `metadata` | object | `{}` | free-form; carries e.g. the raw `response` and `request_kwargs` for subscribers like cassette. |

### `ToolCall`

| field | type | default | notes |
|---|---|---|---|
| `id` | string | — | invocation id (runtime). |
| `name` | string | — | tool name. |
| `arguments` | object | — | invocation arguments. |
| `result` | any \| null | `null` | tool return value. |
| `latency_ms` | number \| null | `null` | runtime. |
| `trace_id` | string | `""` | as above. |
| `ts` | timestamp \| null | `null` | runtime. |
| `metadata` | object | `{}` | free-form. |

### `GuardrailDecision` (emitted by `cendor-guardrails`, not `instrument()`)

A third bus event, emitted by the `guardrails` tool whenever a guardrail trips or flags. Unlike
`LLMCall`/`ToolCall` (emitted by `core`'s `instrument()`), this one is produced by a sibling library
— but it rides the same bus, and `acttrace` chains it as a `guardrail_decision` entry by **duck
typing** (`guardrail`/`stage`/`action` present), with no import in either direction. Documented here
because it is part of the cross-language bus vocabulary a port must match.

| field | type | default | notes |
|---|---|---|---|
| `guardrail` | string | — | the guardrail's name. |
| `stage` | string | — | one of `input` \| `tool_call` \| `tool_output` \| `output`. |
| `action` | string | — | `block` \| `redact` \| `flag` (mirrors acttrace's action vocabulary). |
| `reason` | string | `""` | short, human-readable; **never** the raw payload. |
| `agent` | string | `""` | agent name (SDK), when known. |
| `tool` | string | `""` | tool name for the tool stages, when known. |
| `trace_id` | string | `""` | correlation id set by `trace()`; `""` = uncorrelated. |
| `ts` | timestamp | now | when the decision was made. |
| `metadata` | object | `{}` | free-form; see the reserved keys below. |

**`metadata` — no shape change, richer content (v02 wave 3).** The event shape is unchanged; two
reserved keys now carry policy provenance when present, so no port/acttrace edit is required — a
consumer reads them like any other metadata:

- `policy_hash` (string, `"sha256:<hex>"`) and `policy_version` (string) — stamped on every decision
  from a guardrail built by `load_policy()` (config-as-data). They let an audit chain prove *which*
  policy file was active when a call was gated. They come from the guardrail's static
  `Guardrail.metadata`, which the engine merges under the per-call `Context.metadata` (context wins a
  key clash).
- Hosted-rail adapters (`bedrock_guardrail` / `azure_content_safety` / `model_armor`) still emit a
  plain `guardrail_decision`: the vendor performs the check, but the evidence is local — the `reason`
  names *which* cloud policy fired, never the payload ("cloud check, local evidence").

A port must emit the same field names/conventions (`snake_case` ↔ `camelCase`) so an audit chain
written in one language records byte-identical `guardrail_decision` entries as the other.

## Serialization notes for ports

- These are in-memory event shapes; the **wire** formats that must interoperate are defined by the
  consumers: recorded runs by the [Cassette file format](cassette-format.md), audit entries by the
  [acttrace chain](acttrace-chain.md). Where those persist a subset, the field **names and conventions
  here are canonical**.
- `Money` is always a decimal string, never a float, in any serialized form.
- `Usage` subset conventions (`cached ⊆ input`, `reasoning ⊆ output`, `cache_write` separate) are part
  of the contract, not an implementation detail — they define what the numbers mean to the price formula.
- Field-name mapping across languages follows the [API parity rules](api-parity.md) (`snake_case` ↔
  `camelCase`); the *type* names (`Money`, `Usage`, `LLMCall`, `ToolCall`) are identical everywhere.
