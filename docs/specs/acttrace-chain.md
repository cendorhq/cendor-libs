# acttrace chain

**Spec version:** `acttrace-chain/1` · **Status:** stable · **Implemented by:** `cendor-acttrace` (package 1.x)

A tamper-evident audit log: a hash chain of JSONL entries, offline-verifiable, optionally HMAC-signed. A
chain written by one language must `verify()` in another, so the **canonical bytes that are hashed** are
pinned exactly here.

> **The one thing to internalize:** there are **two** serializations. (a) The on-disk JSONL line is
> written with default JSON (insertion-ordered keys, spaces after `,` and `:`). (b) The bytes that are
> *hashed* are a separate, compact, key-**sorted** serialization of a four-field subset. `verify()`
> re-derives (b) from the parsed *values* of (a), so on-disk whitespace and key order are irrelevant to
> verification — only field values and the canonical recipe below matter.

## Entry

One JSONL line per entry (`\n`-separated; the trailing newline is a separator, not hashed). Fields, in
the reference writer's on-disk order:

| field | type | meaning |
|---|---|---|
| `seq` | int | monotonic from `0`. |
| `ts` | string | timestamp, ISO-8601 UTC in the reference (`…+00:00`). **Opaque to the hash** — hashed as an arbitrary string; a port need not match Python's format, but must hash and re-read the exact string it wrote. |
| `type` | string | `audit_open` · `decision` · `decision_end` · `decision_record` · `llm_call` · `tool_call` · `context_assembly` · `human_oversight` · `policy_flag` (open set). |
| `payload` | object | type-specific (below); redacted before hashing. |
| `prev_hash` | string | previous entry's `hash`; `"0"×64` (GENESIS) for the first entry. |
| `hash` | string | 64-char lowercase hex, per [Hashing](#hashing). |
| `sig` | string | HMAC-SHA256 hex of `hash` under the signing key, or `""` when unsigned. |

The first entry of every log is `type: "audit_open"`, `payload: {"system": …, "risk_tier": …}`.

## Hashing

```
body = JSON({ "payload": <payload>, "seq": <int>, "ts": <string>, "type": <string> },
            sort_keys=true, separators=(",",":"), ensure_ascii=false)   // keys sorted at EVERY level
hash = lowercase_hex( SHA256( UTF8( prev_hash + body ) ) )
```

- **`prev_hash` is prepended as raw text, not a JSON field.** The hashed object contains exactly four
  keys — `payload`, `seq`, `ts`, `type` (that alphabetical order results from `sort_keys`). `prev_hash`,
  `hash`, and `sig` are **not** in the hashed object.
- Canonical JSON: keys sorted ascending by Unicode code point **recursively** (including inside
  `payload`); no whitespace; non-ASCII emitted as raw UTF-8 (never `\u`-escaped); standard escaping for
  `"`, `\`, and control characters.
- GENESIS `prev_hash` for the first entry is 64 ASCII `0` characters.

## Signing (optional)

```
sig = lowercase_hex( HMAC_SHA256( key, UTF8( hash ) ) )   // signs the 64-char hex STRING, not the digest bytes
```

The key is the UTF-8 bytes of the passphrase. Unsigned entries carry `sig: ""` (a missing `sig` is
treated as `""`). There is **no key-id field**.

## Export header (`_meta`)

`export()` writes a first line `{"_meta": { … }}` before the entries (raw appended logs have no `_meta`):

```jsonc
{ "_meta": {
    "system": "…", "risk_tier": "limited",
    "framework": "eu_ai_act" | null, "controls_covered": ["…"],
    "summary": { "decisions": 0, "llm_calls": 0, "tool_calls": 0, "context_assemblies": 0,
                 "human_oversight": 0, "policy_flags": 0,
                 "flags_by_action": {…}, "flags_by_severity": {…} },
    "head_hash": "<final chain head>",   // completeness claim ↓
    "entries": 0,                        // entry count — enables truncation detection
    "disclaimer": "Evidence to support compliance — not legal advice.",
    "sig": "…"                           // signed logs only; see below
} }
```

`_meta.sig` = `HMAC_SHA256(key, canonical({system, risk_tier, head_hash, entries}))` — only those four
completeness fields are signed (canonical order: `entries`, `head_hash`, `risk_tier`, `system`).
`framework`, `controls_covered`, `summary`, and `disclaimer` are **not** signed.

## verify(path, \*, key=…, expected_head=…, expect_entries=…)

Returns `(ok: bool, detail: str)`; never raises on missing/corrupt input. Starting from `prev = GENESIS`,
for each entry line (skipping a leading `_meta` line, remembering it):

1. Recompute `expected = hash(prev, {seq, ts, type, payload})` from the parsed values.
2. `prev_hash != prev` → **broken link** (catches reordering, mid-chain insert/delete).
3. `hash != expected` → **tampered** (catches edits to any hashed field).
4. If `key` given: recompute `HMAC(key, hash)` and constant-time compare to `sig` → **bad signature**.
5. Advance `prev = hash`.

Then completeness: the final `prev` must equal `want_head` (the `expected_head` argument if given, else
`_meta.head_hash`), and the entry count must equal `want_n` (`expect_entries` else `_meta.entries`) —
this is what detects **tail truncation**, which a bare chain cannot. Without a `key`, the in-file `_meta`
is untrusted (an attacker could drop the tail and rewrite `head_hash`/`entries`); **with** a `key`, a
signed log requires a present and valid `_meta.sig`, so a stripped or forged header fails.

## Payload normalization & redaction (before hashing)

Payloads are normalized to plain JSON before hashing: datetimes → ISO strings, `Money` → `"{amount}
{currency}"` (never a float), tuples → arrays, object keys stringified, other objects → their fields or
`str()`. Then, if redaction is on (default), a policy-driven scan scrubs categories whose action is
`redact` or `block`, replacing matched spans with the literal `<redacted>` **before** the hash is
computed — so the chain is over the redacted payload and sensitive values never enter it raw. (Default
policy: `secret` and `email` → redact; others → flag.) `llm_call` payloads record provider/model/usage/
cost/latency only — **never message content**.

**Additive correlation fields (optional, in-context only).** When OpenTelemetry is installed and a
span is active at append time, entries also carry `otel_trace_id` (32-hex) + `otel_span_id` (16-hex),
so an entry can be cross-referenced with an APM trace. Since acttrace 1.9 / 0.10, entries appended
inside a `trace(run_id)` scope (as the SDK's `run()` establishes) additionally carry `run_id`
(Cendor's ambient run id — a trace-aware tool's fallback join to a run when no OTel span was active).
All three are stamped into the payload **before** hashing (so they are inside the verified chain) and
are **omitted when absent** (no active span / outside a run scope) — so the default local-first chain
is byte-identical across languages. A port must stamp them under the same conditions to interoperate.

## Reimplementation traps (must match to interoperate)

1. **int vs float — the biggest hazard.** Python serializes `1000` as `1000` and `1000.0` as `1000.0`;
   JavaScript's single number type would emit `1000` for both, changing the bytes and the hash. A port
   must preserve the int/float distinction for numeric payload values (budgets, token counts, latency,
   cost). Large integer ids beyond 2^53 need care.
2. **Recursive key sorting** at every nesting level — `JSON.stringify` does not sort; a canonicalizer must.
3. **`prev_hash` is text-prepended, not a hashed field.** Putting it inside the object will not match.
4. **Signature input is the hex `hash` string** (UTF-8 bytes of the 64 hex chars), not the raw digest.
5. **`ensure_ascii=false` / raw UTF-8**, and no Unicode normalization is applied — strings hash as-is.
6. **`NaN`/`Infinity`**: Python emits the literal tokens (invalid JSON); avoid non-finite floats in
   payloads or define explicit handling.
7. **Unsigned = `""`, not `null`.** `_meta.sig` covers only four fields (§export header).
8. **No in-band format version.** The wire format carries no `version` field today; consumers pin the
   spec version (`acttrace-chain/1`) out of band. A future revision that adds an in-band version is a
   breaking change and will bump this spec.
