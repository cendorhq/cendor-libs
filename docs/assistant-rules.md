# Rules files for your AI assistant

A **rules file** is a short cheatsheet your assistant reads on every edit, so it writes correct Cendor
call-shapes without you pasting the [trap sheet](for-ai-assistants.md) each time. Drop one of the
blocks below into **your own** repo. Each carries the same cheatsheet: which library does what, the
one call that matters, and the shapes assistants most often get wrong. Keep them short on purpose — an
over-long rules file gets truncated or ignored.

> **Shortcut.** `npx @cendor/init` (or `uvx cendor-init`) writes the right file(s) for you — detected
> from your repo, idempotent, never clobbering your own content. The blocks below are exactly what it
> writes; they're also here to paste by hand. See [init CLI & doctor](assistant-init.md).

> These are a *different artifact* from Cendor's own maintainer `CLAUDE.md` (which says things like
> "never create `__init__.py`"). Don't copy that one — it's about developing Cendor, not calling it.

## What init writes (and where)

One command, five targets, four distinct blocks — Windsurf reuses the `AGENTS.md` body:

| Assistant | File | Block below |
|---|---|---|
| GitHub Copilot | `.github/copilot-instructions.md` | Copilot |
| Cursor | `.cursor/rules/cendor.mdc` | Cursor |
| Cross-tool (Cursor, Codex, others) | `AGENTS.md` | AGENTS.md |
| Claude Code | a marked section in `CLAUDE.md` | Claude Code |
| Windsurf | `.windsurf/rules` | *(a copy of the AGENTS.md body — Windsurf reads that shape)* |

So the four blocks below cover all five files: `init` writes `.windsurf/rules` from the same
`AGENTS.md` cheatsheet rather than a fifth variant.

## Copilot

**GitHub Copilot** → `.github/copilot-instructions.md` (repo-wide). For a monorepo, you can instead
scope rules to paths with `.github/instructions/*.instructions.md` files carrying an `applyTo` glob
in frontmatter.

```md
# Using Cendor (cendor.* / @cendor/*) correctly

Cendor is offline-first plumbing for LLM apps — Python `cendor.*` (PyPI), TypeScript `@cendor/*`
(npm), Apache-2.0. Wrap the provider client **once** with `instrument()`; budgets, gating, testing,
and audit all plug into one event bus. Every public symbol ships an inline `@example` + a
correct-shape type — trust the editor's hover/completion over a guess.

Which library, and the one call that matters (Python shown; TS mirrors it in camelCase — see traps):
- Cap / attribute spend → **tokenguard**: `@budget(usd=0.5, on_exceed="raise")`, `track(...)`, `report()`
- Fit a prompt to a token budget → **contextkit**: `Context(budget_tokens=8000, model="gpt-4o").assemble()`
- Losslessly shrink a payload → **squeeze**: `small, handle = compress(x, kind="auto")`
- Block / redact unsafe input+output → **guardrails**: `rules.keyword_deny([...], action="block")`
- Record once, replay offline in tests → **cassette**: `@cassette.use("tests/x.json")`
- PII/secret detection + tamper-evident audit → **acttrace**: `AuditLog(system="support", risk_tier="limited")`
- Token count / price / instrument → **core**: `instrument(OpenAI())`, `tokens.count(msgs, model="gpt-4o")`
- A whole governed agent loop → **cendor-sdk**: `Agent(name=…, model=…, guardrails=[…], max_usd=0.5)`; `run(agent, "hi")`

Call shapes that are easy to get wrong:
- `instrument()` wraps the client **once**, not per call.
- TS `budget` is **curried**: `budget(cfg)(fn)` — never `budget(cfg, fn)`. Python `budget(...)` takes keyword args and is both a decorator and a context-manager.
- `prices.estimate` — Python positional `prices.estimate(model, input_tokens, output_tokens=200)`; TS options object `prices.estimate(model, inputTokens, { outputTokens: 200 })`.
- Money is `Decimal` / `decimal.js`, never `float` / `number`.
- `Context.assemble()` is sync in Python (`aassemble()` for async), async in TS (`await`).
- Guardrail actions are `block | redact | flag` (no `warn`); PII/secrets are acttrace detectors, not guardrail rules.
- Session store lives in the SDK, casing differs: Python `SQLiteSessionStore`, TS `SqliteSessionStore`.
- TS tokenguard sinks live at the `@cendor/tokenguard/sinks` subpath.
- Python is a PEP 420 namespace — `from cendor.tokenguard import budget`; no top-level `cendor` module.
- Provider SDKs are optional (Python extras, TS peer deps) — install only what you call.
- SDK provider keys: the SDK builds the client, so use the provider's standard env var (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) or `Agent(api_key=…)` / a pre-built `client=`. There is no Cendor key config.
- Telemetry: **do not write any**. With OpenTelemetry installed and a provider configured in the app, Cendor emits `gen_ai.*` call spans, spend counters, an `agent.run` tree per SDK `run()`, and `governance.*` decisions on its own (core ≥ 1.13 / 0.15, sdk ≥ 1.19 / 0.22). Never add `use_span_emitter()` / `use_sink(OTelSink())` / `live_spans()` unless the user asks for explicit control — and never invent an endpoint, key or exporter: Cendor has none, it emits into the app's own provider.
- The off switch is the env var `CENDOR_TELEMETRY=off` (process-wide, no code change); `CENDOR_DEBUG_TELEMETRY=1` prints one line saying whether a provider was detected. With no provider (or no OTel installed) everything is a silent no-op — that is correct, not a bug.
- `AuditLog(system=…)` is the *governance* line; its OTel mirror auto-attaches (pass `mirror=False` to opt out). The mirror is an **operational copy** — `verify()` runs on the hash-chained file. The automatic `governance.*` spans are operational signals too, never "an audit trail".

Honest limits: deterministic guardrails don't stop novel adversarial attacks; acttrace produces
*evidence*, not a compliance guarantee. Full reference: https://cendor.ai/docs/for-ai-assistants
```

## Cursor

**Cursor** → `.cursor/rules/cendor.mdc` (a project rule; the `globs` decide when it attaches).

```md
---
description: How to call Cendor (cendor.* / @cendor/*) correctly
globs: ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js"]
alwaysApply: false
---
Cendor is offline-first plumbing for LLM apps — Python `cendor.*`, TypeScript `@cendor/*`. Wrap the
provider client **once** with `instrument()`; budgets, gating, testing, and audit plug into one bus.
Every public symbol ships an inline `@example` — trust the editor's hover over a guess.

Which library, and the one call that matters (Python; TS mirrors it in camelCase — see traps):
- Cap / attribute spend → **tokenguard**: `@budget(usd=0.5, on_exceed="raise")`, `track(...)`, `report()`
- Fit a prompt to a token budget → **contextkit**: `Context(budget_tokens=8000, model="gpt-4o").assemble()`
- Losslessly shrink a payload → **squeeze**: `small, handle = compress(x, kind="auto")`
- Block / redact unsafe input+output → **guardrails**: `rules.keyword_deny([...], action="block")`
- Record once, replay offline → **cassette**: `@cassette.use("tests/x.json")`
- PII/secret detection + tamper-evident audit → **acttrace**: `AuditLog(system="support", risk_tier="limited")`
- Token count / price / instrument → **core**: `instrument(OpenAI())`, `tokens.count(msgs, model="gpt-4o")`
- A governed agent loop → **cendor-sdk**: `Agent(name=…, model=…, guardrails=[…], max_usd=0.5)`; `run(agent, "hi")`

Traps: `instrument()` once, not per call. TS `budget` is curried — `budget(cfg)(fn)`, never
`budget(cfg, fn)`. `prices.estimate` is positional in Python (`output_tokens=…`) but takes a
`{ outputTokens }` object in TS. Money is `Decimal`/`decimal.js`, never `float`/`number`.
`Context.assemble()` is sync in Python (`aassemble()` async), `await` in TS. Guardrail actions are
`block | redact | flag` (no `warn`); PII/secrets are acttrace detectors, not guardrail rules. Session
store is in the SDK, casing differs (`SQLiteSessionStore` / `SqliteSessionStore`). TS tokenguard
sinks: `@cendor/tokenguard/sinks`. Python is a PEP 420 namespace (`from cendor.tokenguard import
budget`). Provider SDKs are optional (extras / peer deps). SDK provider keys: the provider's standard
env var (`OPENAI_API_KEY`) or `Agent(api_key=…)`, never a Cendor key config. Deterministic guardrails
don't stop novel attacks; acttrace is evidence, not a guarantee. Full reference:
https://cendor.ai/docs/for-ai-assistants
```

## AGENTS.md

**AGENTS.md** (the cross-tool standard Cursor, Windsurf, and others read) → paste this as a section
into your repo's `AGENTS.md`. `init` also writes this same body to `.windsurf/rules`.

```md
## Cendor (cendor.* / @cendor/*)

Offline-first plumbing for LLM apps. Wrap the provider client **once** with `instrument()`; budgets,
gating, testing, and audit plug into one bus. Every symbol ships an inline `@example` — trust the
editor's hover over a guess.

Which library, and the one call that matters (Python; TS mirrors it in camelCase — see traps):
- Cap / attribute spend → **tokenguard**: `@budget(usd=0.5, on_exceed="raise")`, `track(...)`, `report()`
- Fit a prompt to a token budget → **contextkit**: `Context(budget_tokens=8000, model="gpt-4o").assemble()`
- Losslessly shrink a payload → **squeeze**: `small, handle = compress(x, kind="auto")`
- Block / redact unsafe input+output → **guardrails**: `rules.keyword_deny([...], action="block")`
- Record once, replay offline → **cassette**: `@cassette.use("tests/x.json")`
- PII/secret detection + tamper-evident audit → **acttrace**: `AuditLog(system="support", risk_tier="limited")`
- Token count / price / instrument → **core**: `instrument(OpenAI())`, `tokens.count(msgs, model="gpt-4o")`
- A governed agent loop → **cendor-sdk**: `Agent(name=…, model=…, guardrails=[…], max_usd=0.5)`; `run(agent, "hi")`

Traps: `instrument()` once, not per call. TS `budget` is curried — `budget(cfg)(fn)`, never
`budget(cfg, fn)`. `prices.estimate` is positional in Python, `{ outputTokens }` object in TS. Money
is `Decimal`/`decimal.js`, never `float`/`number`. `Context.assemble()` is sync in Python
(`aassemble()` async), `await` in TS. Guardrail actions `block | redact | flag` (no `warn`);
PII/secrets are acttrace detectors, not guardrail rules. Session store is in the SDK, casing differs
(`SQLiteSessionStore` / `SqliteSessionStore`). TS tokenguard sinks: `@cendor/tokenguard/sinks`.
Python is a PEP 420 namespace. Provider SDKs are optional. SDK provider keys: the provider's standard
env var (`OPENAI_API_KEY`) or `Agent(api_key=…)`, never a Cendor key config. Deterministic guardrails
don't stop novel attacks; acttrace is evidence, not a guarantee. Full reference:
https://cendor.ai/docs/for-ai-assistants
```

## Claude Code

**Claude Code** → paste this section into your repo's `CLAUDE.md`.

```md
## Calling Cendor (cendor.* / @cendor/*)

Offline-first plumbing for LLM apps. Wrap the provider client **once** with `instrument()`; budgets,
gating, testing, and audit plug into one bus. Every symbol ships an inline `@example` — prefer the
editor's hover to a guess.

Which library, and the one call that matters (Python; TS mirrors it in camelCase — see traps):
- Cap / attribute spend → **tokenguard**: `@budget(usd=0.5, on_exceed="raise")`, `track(...)`, `report()`
- Fit a prompt to a token budget → **contextkit**: `Context(budget_tokens=8000, model="gpt-4o").assemble()`
- Losslessly shrink a payload → **squeeze**: `small, handle = compress(x, kind="auto")`
- Block / redact unsafe input+output → **guardrails**: `rules.keyword_deny([...], action="block")`
- Record once, replay offline → **cassette**: `@cassette.use("tests/x.json")`
- PII/secret detection + tamper-evident audit → **acttrace**: `AuditLog(system="support", risk_tier="limited")`
- Token count / price / instrument → **core**: `instrument(OpenAI())`, `tokens.count(msgs, model="gpt-4o")`
- A governed agent loop → **cendor-sdk**: `Agent(name=…, model=…, guardrails=[…], max_usd=0.5)`; `run(agent, "hi")`

Traps: `instrument()` once, not per call. TS `budget` is curried — `budget(cfg)(fn)`, never
`budget(cfg, fn)`. `prices.estimate` is positional in Python, `{ outputTokens }` object in TS. Money
is `Decimal`/`decimal.js`, never `float`/`number`. `Context.assemble()` is sync in Python
(`aassemble()` async), `await` in TS. Guardrail actions `block | redact | flag` (no `warn`);
PII/secrets are acttrace detectors, not guardrail rules. Session store is in the SDK, casing differs
(`SQLiteSessionStore` / `SqliteSessionStore`). TS tokenguard sinks: `@cendor/tokenguard/sinks`.
Python is a PEP 420 namespace. Provider SDKs are optional. SDK provider keys: the provider's standard
env var (`OPENAI_API_KEY`) or `Agent(api_key=…)`, never a Cendor key config. Deterministic guardrails
don't stop novel attacks; acttrace is evidence, not a guarantee. Full reference:
https://cendor.ai/docs/for-ai-assistants
```

## Or hand the whole docs to your assistant

If your assistant can read a URL, point it at the full docs bundle instead of a snippet:
[`cendor.ai/llms-full.txt`](https://cendor.ai/llms-full.txt) concatenates every docs page (libraries
+ SDK), each section prefixed with its canonical URL. [`cendor.ai/llms.txt`](https://cendor.ai/llms.txt)
is the curated index. Both are generated at site build, so they never drift from the docs.

## Honest limits

- A rules file is a **static snapshot** — you paste it once, so it's only as current as the day you
  pasted it. For a live lookup, connect the [MCP server](assistant-mcp.md) (agent mode) or trust the
  types Cendor ships in every package (inline `@example` + correct-shape signatures — always the
  version you actually have).
- The blocks are kept **short on purpose**. They cover the common traps, not the whole API — the
  [trap sheet](for-ai-assistants.md) and per-library docs are the full reference.
- These rules teach call **shapes**, never performance numbers. Every benchmark-backed claim lives in
  [Benchmarks](benchmarks.md); `acttrace` produces *evidence*, not a compliance guarantee.
