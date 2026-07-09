# Changelog — cendor-guardrails

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.1.0] — Unreleased
Maturing the Gate from a deterministic-only v1.0 into a detection-tier suite (plan-guardrails-v02). Additive and backward-compatible.

### Added (Wave 1 — execution-model maturity + judge helpers)
- **Per-guardrail `timeout` + `on_error`** on `Guardrail` / `@guardrail` / `rules.custom` / `rules.llm_judge`. `timeout` (seconds) bounds a slow bring-your-own check (async via `asyncio.wait_for`; sync via a worker thread); `on_error` is `"fail_closed"` (default — an errored check is treated as a block) or `"fail_open"` (record a flag and proceed). Either way the failure is emitted as a `GuardrailDecision` — the audit chain records that a check couldn't run, never a swallowed exception. The reason carries the exception type/message, never the payload. The `llm_judge`/`custom` factories default the policy from the action (block → closed, flag → open).
- **`scoped(guardrails)`** — a context manager that gates every instrumented call for the block's duration, scoped to the current execution context (`contextvars`), not process-global. Closes the "process-global `install()`" wart for door-1 users on a concurrent server; nests cleanly.
- **`cendor.guardrails.judge` helpers** — `verdict_prompt(policy)` (a strict-JSON verdict system prompt), `parse_verdict(text)` (strict-JSON → `Verdict`; malformed output raises so `on_error` decides — a garbled judge never silently passes), and `judge(respond, policy)` to compose them into a check for `rules.llm_judge`. The judge call rides an instrumented client, so its own spend lands in tokenguard/acttrace.

### Added (Wave 2 — local classifiers, language, hosted moderation)
- **Opt-in detection-tier adapters** in `cendor.guardrails.adapters`, re-exported as `rules.*` (each rides a bring-your-own dependency or client — never a hard dep; the base package stays deterministic + local-first):
  - `rules.classifier(classify)` — the generic, license-agnostic local-classifier contract: wrap any `classify(text) -> score | {label: score} | bool` and trip over a `threshold`.
  - `rules.prompt_guard(model=…)` — a **prompt-injection classifier adapter** behind the optional `[promptguard]` extra (lazy `transformers`). Model weights are **never bundled** — you download the (license-gated; Meta's Llama Prompt Guard 2 is Llama-Community-Licensed) model yourself. **No jailbreak-detection claim**: `benchmarks/eval_promptguard.py` is the reproduction harness; a detection rate is published only after it is run on a named dataset.
  - `rules.language(allowed)` — trips on an off-list language (a language-switch bypass guard); BYO `detect` or the optional `[langid]` extra.
  - `rules.openai_moderation(client)` — OpenAI's free, non-LLM moderation endpoint (your key).
- **New optional extras** `[promptguard]` (transformers) and `[langid]` (py3langid). **Threat-model** documentation (the tier-0→4 model + documented bypasses; defense in depth) in docs/guardrails.md.

## [1.0.0] — 2026-07-09
### Added
- First release of `cendor-guardrails` — the **Gate** in the Cendor pipeline (`contextkit → squeeze → tokenguard → guardrails → cassette → acttrace`). Define a deterministic check and attach it to one of four intervention points — `input`, `tool_call`, `tool_output`, `output` — matching Azure Foundry's intervention points and OpenAI's four decorator types.
- **The abstraction** — `Guardrail(name, stages, check)`, the `@guardrail(stage=…)` decorator, and a `check(payload, ctx) -> Verdict | None` contract (sync **or** async). A `Verdict` trips with `action="block" | "redact" | "flag"` and an optional `replacement`; `None` passes. `block` is fail-closed (raises `GuardrailTripped`); `redact` replaces the payload and continues; `flag` records and continues.
- **Deterministic built-in rules** (`cendor.guardrails.rules`) — `keyword_deny`, `regex_rule`, `url_allowlist` / `url_deny`, `length_bounds` (char + exact token bounds via `cendor.core.tokens`), `json_schema` (a minimal `type`/`required`/`properties`/`items` validator, no heavy dependency), and `custom`. Regex/arithmetic only: microseconds, offline, $0.
- **Evidence, not just enforcement** — every trip or flag emits a `GuardrailDecision` on the `cendor.core` bus. `acttrace` chains it as a tamper-evident `guardrail_decision` entry with no import in either direction (duck-typed, like contextkit's `AssemblyReport`). The decision carries the guardrail name, stage, action, and a short reason — never the raw payload.
- **Three ways to use it, all offline** — pure `apply()` / `evaluate()` to gate a payload directly; `install()` to register **one** `cendor.core` interceptor so every instrumented client call is gated under any framework (input → block/redact-via-`Reroute`; tool_call → block/record; output → post-flight subscriber); and, via `cendor-sdk`, `Agent(guardrails=[…])` for all four in-loop stages.
- **`llm_judge`** ships as an adapter *contract* only — you supply the model call; cendor ships no classifier. Its extra-call latency and cost are stated honestly in the docs.
- **Honest limits** — deterministic checks do **not** stop a novel adversarial attack. There are no jailbreak-detection or PII-catch-rate claims here; PII/secret detection stays in `acttrace`'s detector catalogue (`guard(Policy…)`) so there is one detection engine, not two.
