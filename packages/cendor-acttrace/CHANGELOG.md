# Changelog — cendor-acttrace

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-acttrace` — a tamper-evident, append-only record of every AI decision (what model, what context, what it cost, which tools, and who signed off), mapped to control templates and exportable as an evidence pack. Integrity comes from a hash chain, not a server.
- **Auto-populating** — construct an `AuditLog` and it subscribes to the bus: every LLM/tool call, plus cost (tokenguard) and context decisions (contextkit) on the same stream, becomes an entry with no per-call wiring.
- **Tamper-evident hash chain** — `verify()` catches edits, reordering, **and tail-truncation**; each entry and the `_meta` head are optionally HMAC-signed, and an out-of-band `expected_head=` gives an authoritative completeness guarantee without a key.
- **Decisions & oversight** — `decision()` groups a unit of work; `d.record(...)` and `d.human_oversight(reviewer, action)` capture Art. 14-style sign-off.
- **Compliance evidence packs** — `export(framework=…)` annotates control IDs for **EU AI Act**, **ISO/IEC 42001**, **GDPR**, and **NIST AI RMF** (starting templates, not certified mappings) and writes a `_meta.summary`. PII redaction is on by default (swap in `redactor=`).
- **Auto-flag on redaction** — when the built-in redactor scrubs PII/secrets (`email`, `api_key` incl. `sk-ant-`/`sk-proj-`, `aws_key`, `google_api_key`, `jwt`, `bearer_token`), acttrace appends a `policy_flag` recording *which category* was removed — so "we removed PII" is in the hash chain, not silent.
- **Policy flags (validation)** — `audit.flag(reason, action="blocked", …)` records a tamper-evident `policy_flag` (and returns the chained entry) when your pre-flight guard refuses input that shouldn't be processed — so the *refusal* is auditable, not just the calls that ran.
- **Offline CLI** — `acttrace verify evidence.jsonl --key "…"` re-walks the chain and checks signatures, exiting non-zero if broken.
- Produces **evidence to support** compliance — not legal advice, not a guarantee. Control mappings are starting templates for your compliance team.
