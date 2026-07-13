# cendor-acttrace

A tamper-evident, append-only record of every AI decision — what model, what context, what it
cost, which tools, and who signed off — mapped to control templates and exportable as an evidence
pack. No database, no infra: integrity comes from a hash chain, not a server.

**Detect and prove: 20 categories of secrets, PII & special-category data — block / warn / redact in one line, offline — every decision in a hash chain you can verify without a server.**

![PyPI](https://img.shields.io/pypi/v/cendor-acttrace) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-acttrace`

Using an AI coding assistant? `npx @cendor/init` (TS) / `uvx cendor-init` (Python) wires it up — or point it at [cendor.ai/docs/for-ai-assistants](https://cendor.ai/docs/for-ai-assistants).

```python
from cendor.core import instrument
from cendor.acttrace import AuditLog

client = instrument(OpenAI())
audit = AuditLog(system="loan_triage", risk_tier="high", signing_key="…")  # auto-subscribes

with audit.decision(input=application, actor="agent") as d:
    resp = client.chat.completions.create(model="gpt-4o", messages=msgs)  # auto-logged
    d.record(model="gpt-4o", prompt_id="triage@v3")          # cost/context captured for free
    d.human_oversight(reviewer="ops@bank", action="approved")

audit.export("evidence_q3.jsonl", framework="eu_ai_act")     # evidence pack (also nist_rmf)
```

```bash
acttrace verify evidence_q3.jsonl --key "…"   # re-walks the chain + checks signatures; non-zero if broken
```

## Highlights

- **Offline detection engine + policy** — a validator-gated `Detector` registry spanning secrets, PII, financial, government-ID, free-text credentials, and GDPR special-category data (20 categories), plus a `Policy` that maps each to `allow` · `flag` · `redact` · `block`. Regex + local checksums (Luhn / IBAN mod-97 / Verhoeff / ABA) — no model, no network, no account. Presets: `Policy.default()` / `gdpr()` / `pci()` / `strict()`.

```python
from cendor.acttrace import scan, redact, Policy

scan("card 4111 1111 1111 1111 for alice@example.com")     # -> [Finding(credit_card…), Finding(email…)]
cleaned, findings = redact({"note": "ping alice@example.com"}, Policy.default())
# cleaned == {"note": "ping <redacted>"} — findings report counts, never the raw value

audit = AuditLog(system="triage", policy=Policy.gdpr())    # special-category → block, PII → redact
```

- **Enforce + record in one line** — `guard(policy, audit=…)` returns a **dual-shape** interceptor (1.5.0): install it on `core`'s seam yourself, or use it as a context manager (`with guard(...):`) that installs/removes itself around the block. It *enforces* the policy (block a disallowed call before it runs, redact-before-send, or flag) and `acttrace` *records* the decision as a tamper-evident `policy_flag`. Recorder and enforcer stay separate — `core` is what stops the call. `resolve_findings()` exports the per-category action resolution for composers.

```python
from cendor.core.instrument import add_interceptor
from cendor.acttrace import AuditLog, Policy, guard, PolicyViolation

audit = AuditLog(system="support_bot", risk_tier="high", signing_key="ops-key")
add_interceptor(guard(Policy.gdpr(), audit=audit))         # a special-category call is blocked + recorded
```

- **Auto-populating** — construct an `AuditLog` and it subscribes to the bus: every LLM/tool call, plus cost (`tokenguard`) and context decisions (`contextkit`) on the same stream, becomes an entry — no per-call wiring.
- **Bounded memory for long-running logs** — `AuditLog(path="audit.jsonl", max_entries=N)` caps the in-memory entry ring so a multi-day agent doesn't grow memory per event. The **file stays the complete, verifiable chain** (`verify()`/`export()` read it), `evicted_from_memory` counts what left memory, and the default (`None`) is unbounded. Bound *together with* `path=`.
- **Tamper-evident hash chain** — `verify()` catches edits, reordering, **and tail-truncation**. The pack's `_meta` head+count catch truncation, but that header is only *authenticated* when the log is **HMAC-signed** and you `verify(key=…)` — the header itself is signed, so a rewritten `_meta` fails. Without a key it's an unauthenticated in-file check, so pass an **out-of-band** `expected_head=` (captured from `log.head` at write time) for an authoritative completeness guarantee. Each entry is optionally HMAC-signed too.
- **Decisions & oversight** — `decision()` groups a unit of work; `d.record(...)` and `d.human_oversight(reviewer, action)` capture Art. 14-style sign-off.
- **Compliance evidence packs** — `export(framework=…)` annotates control IDs for **EU AI Act**, **ISO/IEC 42001**, **GDPR**, and **NIST AI RMF** (starting templates, not certified mappings), and a `_meta.summary` (counts of decisions, oversight, flags by action/severity) gives a reviewer the at-a-glance read first. PII redaction on by default (swap in `redactor=`).
- **Opt-in extras, defaults unchanged** — `enable_locale_pack("uk", "in")` (UK NINO, India Aadhaar — Verhoeff-checked), `enable_entropy_detector()` (high-entropy generic secrets — noisy), and NER-backed name/address redaction via `pip install "cendor-acttrace[ner]"` (Microsoft Presidio, still offline). All strictly opt-in — the zero-extra install stays pure-regex.
- **Auto-flag by policy** — every auto-captured entry is scanned against the full registry, and each detection appends a `policy_flag` with its resolved action (`redacted` / `flagged` / `blocked`), severity, and category — so "we removed / flagged / blocked this" is in the hash chain, not silent (`flag_on_redact=True` by default; a custom `redactor=` owns its own flagging). Detection scrubs the *record*; **block** on `core`'s interceptor seam is the pre-send control.
- **Policy flags (validation)** — `audit.flag(reason, action="blocked", …)` records a tamper-evident `policy_flag` (and **returns** the chained entry) when your pre-flight guard refuses input that shouldn't be processed — so the *refusal* is auditable, not just the calls that ran:

```python
from cendor.core.instrument import add_interceptor, MISS

def guard(call):                                    # your pre-flight policy guard
    if my_policy_disallows(call):                   # YOUR rule
        audit.flag("special-category data", action="blocked")   # acttrace records the refusal
        raise PolicyViolation("blocked")            # your guard enforces it
    return MISS

add_interceptor(guard)   # the blocked call never reaches the bus — flag() is its only record
```

> Produces **evidence to support** compliance — not legal advice, not a guarantee. Control
> mappings are starting templates for your compliance team.

See [`docs/acttrace.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/acttrace.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-acttrace/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
