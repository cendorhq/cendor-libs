# Changelog — cendor-acttrace

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.13.1] — 2026-07-27
**Two live `AuditLog`s on one chain file are refused instead of silently corrupting it.**

Reopening a chain path has been supported since 1.2.2: a process restarts, constructs an `AuditLog`
over the same path, and the chain resumes from the last on-disk entry — measured green. What was never
guarded is two logs alive **at the same time** on one path. Both subscribe to the process-global bus,
so one `LLMCall` is auto-captured twice, and each appends at its own `seq`/`prev_hash` — identical
right after the reopen. The file ends up holding two interleaved chains, and `verify()` reports
`broken link at seq N: prev_hash mismatch`. Nothing warned at the time; the evidence was only
discovered to be broken when someone audited it — the worst possible moment for a governance artifact.

### Fixed
- **`AuditLog(path=…)` now raises if another live `AuditLog` in this process is already writing that
  path**, naming the way out: `detach()` the first log (a process restart does exactly that, and
  resumes the chain), give this log its own file — one per process lifetime, dated or rotated — or
  reuse the log you have. A **sequential** reopen is unchanged, so the restart case and every existing
  reopen test are untouched. This is the same posture as the corrupt-file refusal already in the resume
  path: fail at the line that caused it rather than hand back evidence that will not verify.

Claims are held weakly, so a log dropped without `detach()` cannot strand its path (and it can only be
collected once it has unsubscribed from the bus, which is exactly when the slot should free).
Path-less, in-memory logs are never registered. **Honest limit:** two *processes* appending to one
chain file cannot be detected from inside one of them — one writer per chain file.

## [1.13.0] — 2026-07-26
**Every mirrored entry names the agent that produced it.**

`OTelMirror` stamped `cendor.audit.agent` only on a `guardrail_decision` — the one entry type whose
payload carries an agent. Measured against Cendor Monitor on 2026-07-26: **13 of 386** governance rows
named their agent, so "which agent was blocked" was answerable only by inferring it from step ordering.
On a governance product that is the attribute most worth having.

The mirror now reads the acting agent (and its id, when the app gave one) from `cendor-core`'s ambient
registry and stamps `cendor.audit.agent` / `cendor.audit.agent_id` on **every** entry — including the
types with no agent field at all: a budget block, a decision record, an `llm_call`. The entry's own
payload always wins. acttrace still imports no sibling tool: the SDK registers a provider, core merges
it, the mirror reads it (and an older core without that read degrades to today's behaviour).

Nothing about the hash-chained evidence file changes: this is the **operational copy**.

## [1.12.0] — 2026-07-25
**The mirror wins:** when an `AuditLog` attaches a mirror that emits OpenTelemetry spans, acttrace now
tells `cendor-core` (refcounted; released on `detach()`), so core's new Option C `governance.*` ops
spans stand down while the chained `audit.*` spans are on the wire — one decision, one rendering.

A *custom* mirror that writes elsewhere (your SIEM sink) deliberately does **not** suppress them:
nothing audit-shaped is on the OTel wire in that case, so a telemetry user still needs the ops spans.

### Changed
- Floor: `cendor-core>=1.13` (the `governance_mirrored` seam).

## [1.11.0] — 2026-07-25
**Governance is one line, not four** (see `cendor-core` 1.12.0 for the switch).

### Added
- **`AuditLog(...)` auto-attaches an `OTelMirror`** when you pass no `mirror`, OpenTelemetry is
  installed and `CENDOR_TELEMETRY` isn't `off`. You already declared governance by constructing the
  log; its **operational copy** now reaches the backend you configured with no extra line.
- **`AuditLog(mirror=False)`** — the per-log opt-out ("never mirror this log"). An explicit mirror is
  still used verbatim.

**Unchanged, deliberately:** nothing ever *creates* an `AuditLog` for you; the chain, the file format
and `verify()` are identical; the mirror remains an **operational copy** — the hash-chained file (or a
signed `export()` pack) is still the only artifact `verify()` checks, and a failing mirror is still
swallowed rather than breaking the chain (rule 6).

## [1.10.1] — 2026-07-22

### Added
- **`reset_detectors()`** — restore the detector registry to the built-in defaults, dropping anything
  added by `register_detector` / `enable_entropy_detector` / `enable_locale_pack`. The registry is
  process-global (opt in once at startup); this is the inverse — for turning an opt-in detector back
  off, dynamic reconfiguration, and test isolation (so one test's `enable_entropy_detector()` can't
  leak into the next and scrub, e.g., a high-entropy id from a later audit payload). `register_detector`
  is now idempotent (a detector already present is not added twice).

## [1.10.0] — 2026-07-22
Auto-captured entries read run/decision context from the event, not delivery-time ambient reads.

### Fixed
- Auto-captured `llm_call` / `tool_call` entries now take their `run_id` from the event's own captured `trace_id` and their `decision_id` from context captured at call initiation (via the `cendor-core` ambient seam), instead of re-reading the ambient run/decision scope at delivery time. A streamed call finalized outside the originating run/decision scope is therefore still joined to the right run and chained under the right decision. In-scope chains are byte-identical.
- `budget_event` entries copy the `tokenguard` `BudgetEvent.trace_id` into `run_id`, so a monitor's dual-key join links a budget action to its run.

### Changed
- Requires `cendor-core >= 1.9` (the ambient seam).

## [1.9.0] — 2026-07-22
Governance→run correlation fallback (G-LINK-2). Backward-compatible; the file remains the sole verifiable evidence and the default (no-run-scope) chain is byte-identical.

### Added
- **`run_id` correlation on audit entries.** When an entry is appended inside a `cendor-core` `trace(run_id)` scope (as the SDK's `run()` establishes), that ambient run id is stamped on the entry payload as `run_id` and mirrored as `cendor.audit.run_id`. This lets an observability tool (e.g. Cendor Monitor) join a governance event to its run even when **no OpenTelemetry span was active** at append time — a post-hoc `span_tree`, or an app with no OTel context manager — complementing the existing `otel_trace_id` active-span correlation. Reads `cendor-core`'s own ambient (not OpenTelemetry). **No-op outside a run scope** (`current_trace_id()` is `""`), so the default local-first chain stays byte-identical and matches the TypeScript `@cendor/acttrace` implementation.

## [1.8.0] — 2026-07-20
Compression enters the audit chain (G21) — squeeze's `CompressionEvent` becomes evidence + a span.

### Added
- **`compression` audit entry type** — a `squeeze` `CompressionEvent` (≥ 1.1 / 0.3) on the bus is duck-typed (keys `technique` + `ratio`) into a `compression` chain entry (metadata only: technique, tokens before/after, ratio, store kind, handle id, kind) and mirrored as an `audit.compression` span (`cendor.audit.technique` / `.tokens_before` / `.tokens_after` / `.ratio` / `.store_kind` / `.handle_id` / `.kind`). Metadata-only, so it is **not** in `_AUTO_REDACT_TYPES` (no content to scrub). Framework control mappings added for all four bundled frameworks. Backward-compatible; the file remains the sole verifiable evidence.

## [1.7.0] — 2026-07-20
Mirror completeness: the `OTelMirror` now carries the structured fields an audit-history / monitoring view needs — not just labels, but the numbers. Backward-compatible; the file remains the sole verifiable evidence and the default (no-OTel) chain is byte-identical.

### Added
- **Budget identity + numbers on `audit.budget_event` spans** (G10/G11). The budget's name lands as `cendor.audit.budget` (from `tokenguard`'s new `budget(name=…)`), its description as `cendor.audit.description` (truncated), and the projected-vs-cap figures as dedicated attributes — `cendor.audit.projected_usd` / `cendor.audit.cap_usd` (money as strings, per the `Decimal` rule), `cendor.audit.projected_tokens` / `cendor.audit.cap_tokens` (ints), plus `scope`, `to_model`, and each `track()` tag as `cendor.audit.tag.<key>`. So a monitor shows *which* budget blocked *what*, not just a free-text reason.
- **`audit.llm_call` spans** now carry `cendor.audit.input_tokens` / `output_tokens` / `reasoning_tokens` (ints), `latency_ms`, and `replayed` (bool).
- **`audit.guardrail_decision` spans** now carry `cendor.audit.agent`, `cendor.audit.tool`, and the guardrail's nested `severity` / `policy_version` / `policy_hash` (previously the top-level `severity` only matched a `policy_flag`).
- **`audit.context_assembly` spans** now carry `cendor.audit.budget_tokens` / `used_tokens` (ints) and non-zero per-action block counts (`kept` / `truncated` / `summarized` / `compressed` / `dropped`) — a `compressed` count is squeeze's indirect visibility on the wire (G16).
- **`audit.human_oversight`** carries the reviewer's `note` (truncated); **`audit.audit_open`** carries `risk_tier`; the correlation `otel_span_id` is now exposed as a queryable span attribute (the pivot target).

## [1.6.0] — 2026-07-19
Observability export: stream the audit trail to any OpenTelemetry backend, and correlate entries with your traces. Backward-compatible — the file remains the sole verifiable evidence and the default (no-OTel) chain is byte-identical.

### Added
- **`AuditLog(mirror=…)` + `OTelMirror`.** Attach an optional mirror (a `core.protocols.Sink`) and every chained entry — decisions, `llm_call`/`tool_call`, `guardrail_decision`, `budget_event`, `policy_flag`, `human_oversight` — is *also* sent to it, an **operational copy** for monitoring/alerting/SIEM. `OTelMirror` emits each as an `audit.<type>` OpenTelemetry span (a no-op without OpenTelemetry). The mirror is best-effort — a failing mirror is swallowed and never breaks the chain; `verify()` still runs only on the hash-chained file, which stays the sole evidence. `detach()` flushes/closes a mirror that implements those lifecycle methods.
- **`budget_event` entry type.** `tokenguard`'s pre-flight `BudgetEvent` (blocked/downgraded/clamped) is chained by duck typing, with control-mapping annotations across the bundled frameworks.
- **OpenTelemetry correlation.** When OpenTelemetry is installed and a span is active, auto-captured and explicit entries carry the active span's `otel_trace_id`/`otel_span_id`, so an audit entry can be cross-referenced with an APM trace (a no-op otherwise). See [Observability](https://cendor.ai/docs/observability).

## [1.5.0] — 2026-07-14
The dual-shape guard: `guard()`'s return is now scope-capable, so the SDK can re-export the identical object (`cendor.sdk.guard is cendor.acttrace.guard`). Backward-compatible — the raw interceptor form is unchanged.

### Added
- **`guard()` returns a dual-shape `GuardInterceptor`.** Still the plain pre-call interceptor you install via `core.add_interceptor` (unchanged behavior, same signature), and now *also* a context manager: `with guard(Policy.gdpr(), audit=log): …` installs the interceptor on core's seam on enter and removes it on exit (exactly once each, exception-safe). Enforcement still lives on core's seam — the recorder/enforcer split is intact.
- **`resolve_findings(findings, policy=None)`** — the per-category action resolution `guard()` applies, exported: partitions findings into `{"block": […], "redact": […], "flag": […]}`; with `policy` given, each finding is re-resolved against it (scan under one policy, enforce under another). Composers (like the SDK's pii/secrets bridge) can now honor per-category actions instead of flattening to one.
- `GuardInterceptor` is exported for typing.

## [1.4.2] — 2026-07-11
AI-assistant onboarding: inline Type Teach ships inside the package, plus the bundled integration guide. No runtime behavior change for correct code.

### Added
- Inline `@example` + a correct-shape signature on public symbols, so your editor's language server (and agent-mode assistants) is handed the right call as you type — the wrong shape becomes a type error whose message states the right one.
- `INTEGRATION.md` is now bundled in the installed package — a one-screen "call Cendor correctly" guide. Full trap sheet: https://cendor.ai/docs/for-ai-assistants

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
