# cendor-acttrace

A tamper-evident, append-only record of every AI decision — what model, what context, what it
cost, which tools, and who signed off — mapped to control templates and exportable as an evidence
pack. No database, no infra: integrity comes from a hash chain, not a server.

**Audit-ready evidence in 5 lines — and verifiable offline.**

![PyPI](https://img.shields.io/pypi/v/cendor-acttrace) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-acttrace`

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

- **Auto-populating** — construct an `AuditLog` and it subscribes to the bus: every LLM/tool call, plus cost (`tokenguard`) and context decisions (`contextkit`) on the same stream, becomes an entry — no per-call wiring.
- **Tamper-evident hash chain** — `verify()` catches edits, reordering, **and tail-truncation**. The pack's `_meta` head+count catch truncation, but that header is only *authenticated* when the log is **HMAC-signed** and you `verify(key=…)` — the header itself is signed, so a rewritten `_meta` fails. Without a key it's an unauthenticated in-file check, so pass an **out-of-band** `expected_head=` (captured from `log.head` at write time) for an authoritative completeness guarantee. Each entry is optionally HMAC-signed too.
- **Decisions & oversight** — `decision()` groups a unit of work; `d.record(...)` and `d.human_oversight(reviewer, action)` capture Art. 14-style sign-off.
- **Compliance evidence packs** — `export(framework=…)` annotates control IDs for **EU AI Act**, **ISO/IEC 42001**, **GDPR**, and **NIST AI RMF** (starting templates, not certified mappings), and a `_meta.summary` (counts of decisions, oversight, flags by action/severity) gives a reviewer the at-a-glance read first. PII redaction on by default (swap in `redactor=`).
- **Auto-flag on redaction** — when the built-in redactor scrubs PII/secrets (`email`, `api_key` incl. `sk-ant-`/`sk-proj-`, `aws_key`, `google_api_key`, `jwt`, `bearer_token`) from an auto-captured entry, acttrace appends a `policy_flag` recording *which category* was removed — so "we removed PII" is in the hash chain, not silent (`flag_on_redact=True` by default; a custom `redactor=` owns its own flagging).
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

See [`docs/acttrace.md`](https://github.com/cendorhq/Cendor/blob/main/docs/acttrace.md) · [CHANGELOG](https://github.com/cendorhq/Cendor/blob/main/packages/cendor-acttrace/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/Cendor](https://github.com/cendorhq/Cendor). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
