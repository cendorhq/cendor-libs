# Cassette file format

**Spec version:** `cassette/2` · **Status:** stable · **Implemented by:** `cendor-cassette` (package 1.x) ·
**File `version` field value:** `2`

A cassette is a single JSON file holding one recorded run — every LLM **and** tool call, in order — so
it can be replayed offline, deterministically, with no network or API key. This spec pins the on-disk
bytes precisely so a cassette recorded in one language replays in another.

## File envelope

UTF-8 text. Serialized as pretty JSON — the reference writer uses `json.dumps(payload, indent=2,
ensure_ascii=False)` (2-space indent, raw non-ASCII, key/value separator `": "`, item separator `","`),
with **no key sorting** (top-level insertion order). Exactly two top-level keys, in this order:

```jsonc
{
  "version": 2,        // int. Current = 2. A file with NO version key is read as version 1.
                       // Only {1, 2} are accepted; any other value is a hard error.
  "entries": [ … ]     // array of entry objects, in call/emission order (LLM + tool interleaved)
}
```

## Entry object

Every entry — LLM or tool — has the same six fields, in this order:

| # | field | type | meaning |
|---|---|---|---|
| 1 | `seq` | int | 0-based position in emission order. **Informational** — replay does not match on it. |
| 2 | `kind` | string | discriminator: `"llm"` or `"tool"`. |
| 3 | `request_hash` | string | 64-char lowercase hex SHA-256 of the canonicalized **un-redacted** normalized request (see [Matching](#request-matching--hashing)). |
| 4 | `request` | object | the **redacted** normalized request (only the normalized fields — never the full call kwargs). |
| 5 | `response` | any | the **redacted**, JSON-coerced response. A single object normally; an **array of chunk objects** for a streamed response; a dict for mapping-style providers. |
| 6 | `response_type` | string | `"object"` (SDK-like → replayed as an attribute namespace), `"mapping"` (dict-like → replayed as a dict), or `"envelope"` (a raw-response call → replayed as a value whose `parse()` returns the payload). **v2 only**; absent in v1, where readers default to `"object"`. A reader that does not know a marker **must** fall back to `"object"` — which is what makes `"envelope"` an additive change rather than a format version. |

**The stored `request` is a normalized subset, not the raw call.** For an LLM call:

```jsonc
{ "kind": "llm", "provider": "openai", "model": "gpt-4o",
  "messages": [ … ],   // provider-native message dicts, passed through unchanged (see traps)
  "stream": false }    // bool — v2 ONLY; omitted in v1
```

For a tool call:

```jsonc
{ "kind": "tool", "name": "search",
  "arguments": { "args": [ … ], "kwargs": { … } } }   // always this {args, kwargs} shape
```

No temperature, `max_tokens`, ids, timestamps, usage, cost, latency, or `trace_id` are persisted — the
file is intentionally minimal. (The only volatile field folded in is `stream`.)

## Request matching / hashing

Replay matches a live call to an entry by hashing the **normalized** request. The hash is **distinct
from** the file serialization above:

```
canonical = JSON(request, sort_keys=true, separators=(",",":"), ensure_ascii=false)   // compact, sorted, raw UTF-8
request_hash = lowercase_hex( SHA256( UTF8(canonical) ) )
```

- The hash is computed on the **un-redacted** normalized request; the `request` written to disk is the
  **redacted** version. They deliberately diverge — so two calls that differ only inside a redacted span
  (e.g. two different API keys) still replay to distinct entries and never collide.
- Replay indexes entries by `request_hash` and consumes matches **FIFO per hash**, so repeated identical
  requests replay in recorded order; distinct requests match independently of interleaving. An
  unmatched call, or one call too many for a hash, is an error (never a silent live call in `replay` mode).

## Redaction on write

Both the stored `request` and `response` are scrubbed **after** the match hash is computed. The built-in
scrubber replaces each match of a fixed, ordered set of secret/PII patterns (email, `sk-…` keys, AWS
`AKIA…`, Google `AIza…`, JWT, bearer tokens, and a 32-char-plus opaque-token catch-all) with the literal
string `<redacted>`, recursively through strings inside objects and arrays. Redaction is configurable
(off, or a custom scrubber), but a byte-compatible reimplementation must reproduce the same patterns,
order, and `<redacted>` token to match committed cassettes.

## Streaming

A streamed response has **no separate field or reassembly**: it is stored as `response` holding a JSON
**array of per-chunk objects**, and `request.stream` is `true` (v2). Because `stream` participates in the
hash, a `stream=true` call and a `stream=false` call to the same model never collide on one entry.
`response_type` is still `"object"` for streams. A non-streamed response is stored as a single object.

## Ordering

Entries are one flat list in emission (call-completion) order, LLM and tool calls interleaved. `seq`
records that order but is informational; correctness on replay comes from `request_hash` + FIFO, not
position.

## Reimplementation traps (must match to interoperate)

1. **`messages` are provider-native and passed through unchanged** — the single biggest hashing risk. A
   port must serialize messages to the exact same JSON structure the reference SDK path produced, or the
   `request_hash` differs. The cassette does not normalize message shape.
2. **Two serializations, not one.** File = pretty (`indent=2`, insertion order). Hash = compact
   (`sort_keys`, `separators=(",",":")`). Don't conflate them.
3. **Number/float formatting and object coercion.** Non-JSON-native values are coerced (dict keys
   stringified, tuples → arrays, objects via `model_dump`/`dict`/`to_dict`/`vars`, else `str()`); integral
   floats vs ints and any `str()` fallback are implementation-sensitive spots for byte-compat.
4. **Stored request ≠ its own hash.** The written `request` is redacted; it will not re-hash to
   `request_hash` (which is over the un-redacted form). This is intentional, not corruption.
5. **v1 compatibility.** A v1 file omits `stream` from the request and `response_type` from entries; a
   reader must default `response_type` to `"object"` and hash v1 requests **without** a `stream` key
   (select the normalizer by the file's `version`).
