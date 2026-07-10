# Changelog — cendor-acttrace

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.4.1] — 2026-07-10
Deep-QA fixes: honest `[ner]` availability + a clearer reopen error.

### Fixed
- **`[ner]` now reports availability honestly and fails with a clear error.** The `[ner]` extra installs Presidio + spaCy but no language model. `ner_available()` returns `True` only when a spaCy model is also loadable, and `ner_redactor()` builds an explicit `NlpEngineProvider` and raises a clear `ImportError` (backend missing) / `RuntimeError` (model missing — with the `python -m spacy download en_core_web_sm` hint) instead of letting Presidio shell out to `pip` (which hard-exits in a pip-less venv). Documented the model-download step.
- **Reopening an `export()` evidence pack as a log** now raises a clear "this is a read-only export pack, not an appendable log" error instead of a generic "corrupt or unparseable".

## [1.4.0] — Unreleased
### Added
- **Guardrail decisions now carry their `metadata` into the chain.** The `guardrail_decision` entry gains a `metadata` field, so a decision's provenance is recorded as tamper-evident evidence — notably `cendor-guardrails`' `load_policy()` stamps `policy_hash` / `policy_version`, letting an audit prove **which** policy was active when a call was gated. Still duck-typed (no sibling import); `metadata` defaults to `{}`, so a chain with no metadata is byte-identical to before.

## [1.3.0] — 2026-07-09
### Added
- **Auto-capture of guardrail decisions.** When `cendor-guardrails` is in use, every trip or flag it emits on the `cendor.core` bus is now chained as a tamper-evident `guardrail_decision` entry (recording the guardrail name, stage, action, and reason — never the raw payload). Captured by **duck typing** (`guardrail`/`stage`/`action` present), so acttrace still imports no sibling tool — the same pattern used for contextkit's `AssemblyReport`. No API change; a log with no guardrails in play is byte-identical to before.

## [1.2.2] — 2026-07-08
### Fixed
- **`AuditLog(path=…)` no longer truncates an existing log on construction.** Reopening a log now opens the file in append mode and **resumes the hash chain** from the last on-disk entry (continuing `head` and the sequence counter) instead of restarting from genesis and overwriting the prior entries — a silent data-loss bug that broke long-term retention (EU AI Act Art. 19, HIPAA). A reopen is a **pure resume**: no new `audit_open` marker is emitted, existing entries are preserved, and `verify()` spans the full pre- and post-reopen chain. A fresh/empty log is unchanged (still seeds `audit_open` at seq 0). A corrupt or unparseable tail now raises instead of silently restarting from genesis.

## [1.2.1] — 2026-07-05
### Changed
- Repository moved to `github.com/cendorhq/cendor-libs`; `[project.urls]` updated. No API or behavior change.

## [1.2.0] — 2026-07-05
### Added
- **`AuditLog(max_entries=N)`** bounds the **in-memory** entry ring for long-running logs: once full, the oldest in-memory entry is evicted so memory stays flat. The **file is the source of truth** — the hash chain lives in `head` + the on-disk log, so eviction never touches it; `verify(path, …)` re-walks the full chain and `export()` re-reads the file when memory was bounded, so both stay complete. New `AuditLog.evicted_from_memory` property counts what left memory (never silent), and a `BoundedMemoryWithoutPathWarning` is raised if `max_entries` is set without `path=` (evicted entries would be lost). Default `max_entries=None` is unbounded and **byte-identical** to previous behaviour.

## [1.1.0] — 2026-07-04
### Added
- **Offline detection engine** — a `Detector` registry (`DETECTORS`, `register_detector()`) of validator-gated patterns spanning **20 categories** across six groups: secrets (`api_key`, `aws_key`, `google_api_key`, `github_token`, `slack_token`, `private_key`, `jwt`, `bearer_token`), free-text credentials (`password`), financial (`credit_card`, `iban`, `us_routing`, `swift_bic`), government IDs (`us_ssn`), PII (`email`, `phone`, `ipv4`, `ipv6`, `mac_address`), and GDPR Art.9 special-category data. Loose patterns are gated by local checksums/format checks (Luhn, IBAN mod-97, Verhoeff, ABA, ISO-3166) — regex + arithmetic only, no model, no network.
- **`Policy`** (`allow` · `flag` · `redact` · `block` per category/group) with presets `Policy.default()` / `gdpr()` / `pci()` / `strict()`, and `Finding`.
- **Pure `scan()` / `redact()`** — detect or scrub any str/dict/list independent of the audit chain; `scan()` returns counts and resolved actions, never the raw value.
- **`AuditLog(policy=…)`** — auto-scans every auto-captured payload against the full registry, scrubs `redact`/`block` categories before chaining, and auto-flags each detection with its resolved action/severity. Category-tagged `policy_flag`s now map to specific controls in `export()` (e.g. special-category → GDPR Art.9).
- **`guard(policy, audit=…, on_block=…)` + `PolicyViolation`** — a batteries-included enforcement callable for `core`'s interceptor seam. Per outbound call it resolves each detected category via the policy: **block** → record `policy_flag(action="blocked")` and raise (the call never runs); **redact** → scrub the outbound messages so the *provider* receives cleaned content (via `core`'s `Reroute(messages=…)`) and record `action="redacted"` → proceed (tools have no message-rewrite seam, so a redact on tool arguments is record-only — block is the pre-send control there); **flag** → record and proceed. Recorder/enforcer split intact: `guard()` returns a callable *you* install on the seam. Requires `cendor-core` with `Reroute(messages=…)`.
- **Opt-in extras (defaults unchanged, no new hard deps)** — `enable_locale_pack("uk", "in")` registers locale government-ID detectors (UK NINO prefix-validated, India Aadhaar Verhoeff-checked); `enable_entropy_detector(min_length=, min_entropy=)` adds a noisy high-entropy generic-secret detector (off by default); and the optional `[ner]` extra (`pip install "cendor-acttrace[ner]"`) provides `ner_redactor()` / `ner_available()` for offline NER-backed name/address redaction via Microsoft Presidio. A zero-extra install detects exactly the built-in categories and `default_redactor` is unchanged.

### Changed
- `AuditLog(redact=True)` is now exactly `policy=Policy.default()` — **100% backward compatible**: secrets & `email` are `redact`ed, everything else `flag`ged. `default_redactor` is rebuilt from the registry and scrubs the original six categories byte-for-byte.

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
