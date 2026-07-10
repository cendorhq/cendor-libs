# Cendor for AI coding assistants

You probably arrived here because you told an assistant — Copilot, Claude Code, Cursor, Windsurf —
*"add cost budgeting to my OpenAI calls"* and it reached for Cendor. Cendor is new, so a model that
hasn't seen much of it will guess the call-shape, and a few of our shapes are non-obvious
(`budget(cfg)(fn)` is curried in TypeScript; `prices.estimate` is positional in Python; the SQLite
session store is spelled differently in each language). This page is the **canonical call-shape
reference** — a trap table your assistant (or you) can read once, then get every call right.

You don't have to read this to use Cendor correctly, though. Every public symbol ships an inline
`@example` and a one-line correct-shape in its type signature, so your editor's language server
(Pylance / `tsserver`) — and any agent-mode assistant that reads diagnostics — is handed the right
shape at the moment you type the call. The wrong shape is a compile error whose message states the
right one. This page just makes that knowledge copy-pasteable.

> **How to point your assistant here.** Paste this page's URL (or the trap table below) into your
> assistant's context, or drop the trap table into your repo's `AGENTS.md` /
> `.github/copilot-instructions.md` / `.cursor/rules`. The types teach the rest on install.

> **Or run one command.** `npx @cendor/init` (Node) / `uvx cendor-init` (Python) writes the
> [rules files](assistant-rules.md) into your repo for you — idempotently, never clobbering
> your own content — and can add the MCP config and a working starter. Offline, no key. It ships a
> `doctor` too: `npx @cendor/init doctor` static-checks your wiring (namespace, provider deps,
> `instrument()` once, money-as-`Decimal`, versions) and exits non-zero on hard problems, so it fits CI.

## The trap table

Every row is verified against the current source in both languages. `snake_case` (Python) ↔
`camelCase` (TypeScript) is the default rename; the traps below are where the shapes genuinely
differ or where a plausible guess is wrong.

| Task | Python (`cendor.*`) | TypeScript (`@cendor/*`) | The trap |
|---|---|---|---|
| Instrument a client | `client = instrument(OpenAI())` | `const client = instrument(new OpenAI())` | Wrap the client **once**, not per call. Idempotent, additive, and works on sync / async / **streaming** clients. |
| Budget a function | `@budget(usd=0.50, on_exceed="raise")` — or `with budget(usd=0.50) as b:` | `budget({ usd: 0.50, onExceed: 'raise' })(fn)` — or `withBudget(cfg, cb)` | TS `budget` is **curried**: `budget(cfg)(fn)`, never `budget(cfg, fn)`. Python `budget(...)` is **not** curried — it takes keyword args and is itself a decorator *and* a context manager (there is no `with_budget`). |
| `on_exceed` / `onExceed` | `"raise" \| "block" \| "truncate" \| "downgrade" \| "clamp"` (or a callable) | same set (or a callable) | A fixed union — a typo is a type error, not a silent no-op. |
| Estimate cost | `prices.estimate(model, input_tokens, output_tokens=200)` | `prices.estimate(model, inputTokens, { outputTokens: 200 })` | Python takes `output_tokens` **positionally**; TS requires the `{ outputTokens }` **options object**. Real divergence — don't cross them. |
| Register a model price | `register_model_price(model, input=…, output=…, per="1M")` — from `cendor.sdk` | `prices.register(model, { input, output })` — from `@cendor/core` | `prices.register(...)` does **not** exist in Python. Rates default to **per-1M** tokens (`per="1K"`/`"token"` to change). |
| Count tokens | `tokens.count(messages, model="gpt-4o")` | `tokens.count(messages, 'gpt-4o')` | It's `tokens.count`, not `count_tokens` / `countTokens`. Counts match across languages (`tiktoken` / `js-tiktoken`). |
| Pre-call interceptor | `add_interceptor(fn)` / `remove_interceptor(fn)` (top-level of `cendor.core`) | `addInterceptor(fn)` / `removeInterceptor(fn)` | Not on `bus` — `bus` only has `subscribe` / `unsubscribe` / `emit`. Return a `Reroute(...)` from an interceptor to rewrite the call before it's sent. |
| Money | `Money(Decimal("0.01"))` — never `float` | `new Money(new Decimal('0.01'))` — `decimal.js`, never `number` | Cost / price values are `Decimal` / `decimal.js`. A `float`/`number` is a precision bug, and money-typed params reject it. |
| Assemble context | `msgs = Context(budget_tokens=8000, model="gpt-4o").assemble()` (sync; async is `aassemble()`) | `const msgs = await new Context({ budgetTokens: 8000, model: 'gpt-4o' }).assemble()` (async) | `assemble()` is **sync in Python** (separate `aassemble()` for async) but **async in TS** (no sync form). It's a method on `Context`, not a free function. |
| Compress-to-fit blocks | `Block(docs, evict="compress")` needs the `contextkit[squeeze]` extra | `new Block(docs, { evict: 'compress' })` needs `@cendor/squeeze` installed | Without squeeze present, `evict="compress"` silently falls back to truncation. |
| Compress a payload | `small, handle = compress(content, kind="auto", fidelity="balanced")` | `const [small, handle] = compress(content, { kind: 'auto', fidelity: 'balanced' })` | `kind` ∈ `auto\|json\|logs\|code\|prose`; `fidelity` ∈ `lossless\|balanced\|aggressive`. Returns a `(small, handle)` pair — keep the handle to `expand()`. |
| (De)serialize a handle | `handle.to_dict()` / `Handle.from_dict(d)` | `handle.toDict()` / `Handle.fromDict(d)` | `snake_case` in Python, `camelCase` in TS (the wire keys *inside* the dict stay snake_case). |
| Swap the compression store | `from cendor.squeeze import store` → `use_store(store.SQLiteStore(path))` | `import { SQLiteStore, useStore } from '@cendor/squeeze'` | Store classes are capital-`SQL` `SQLiteStore` / `MemoryStore`; in Python reach them via `cendor.squeeze.store.*` (not top-level). |
| Deterministic guardrail | `rules.keyword_deny([...], action="block")` | `rules.keywordDeny([...], { action: 'block' })` | Names: `regex_rule`/`regexRule` (not `regex`), `custom` (not `custom_rule`), plus `custom_category`, `intent`, `denied_topics`. **PII/secrets are not guardrails rules** — they're acttrace detectors (bridged into the SDK as `rules.pii`/`rules.secrets`). |
| Guardrail action / stages | `action="block" \| "redact" \| "flag"` | `action: 'block' \| 'redact' \| 'flag'` | Four stages: `input`, `tool_call`, `tool_output`, `output`. There is no `warn`. |
| Run a gate directly | `payload, decisions = evaluate(gate, "input", text)`; catch `GuardrailTripped` | `const { payload, decisions } = evaluate(gate, 'input', text)`; catch `GuardrailTripped` | Under the SDK you do **not** call `evaluate` yourself — pass `Agent(guardrails=[…])` and the loop gates all four stages. |
| Local semantic embedder | `embeddings.local_embedder()` (`[embeddings]` extra) | `await embeddings.localEmbedder()` (async; `@huggingface/transformers` peer) | It's `embeddings.localEmbedder`, not `rules.localEmbedder`. |
| Record / replay a run | `@cassette.use("t.json")` (decorator) — or `with cassette.using("t.json"):` | `cassette.use('t.json')` (decorator) — or `await cassette.using('t.json', async () => …)` | `use` is the decorator, `using` is the scope form. `mode` ∈ `auto\|record\|replay\|rerecord`. |
| Session store (SDK) | `SQLiteSessionStore(path)` — capital `SQLite` | `new SqliteSessionStore(path)` — `Sqlite` | Casing **differs across languages**. It lives in the **SDK**, not `cassette` — cassette has no session store. |
| Audit + export evidence | `AuditLog(system="support", risk_tier="limited")`; `audit.export(path, framework="eu_ai_act")` | `new AuditLog('support', { riskTier: 'limited' })`; `audit.export(path, 'eu_ai_act')` | `export` / `verify` hang off the log. `framework` ∈ `eu_ai_act\|gdpr\|iso_42001\|nist_rmf`. There is no top-level `decisions` — group work with `AuditLog.decision()`. |
| Spend sink subpath (tokenguard) | `from cendor.tokenguard import sinks` → `sinks.SQLiteSink(path)` | `import { SQLiteSink } from '@cendor/tokenguard/sinks'` | In TS the sinks live at the **`/sinks` subpath**, not the package root. |
| A governed agent (SDK) | `Agent(name=…, model=…, guardrails=[…], max_usd=0.5)`; `run(agent, "hi")` | `new Agent({ name, model, guardrails: [...], maxUsd: 0.5 })`; `run(agent, 'hi')` | No `budget=` field on `Agent` — the per-agent cap is `max_usd`/`maxUsd`; process-wide budgets use tokenguard's `budget()`. TS ships **OpenAI + Anthropic** first-class; other providers construct lazily. |

A few cross-cutting rules that don't fit a row:

- **Provider SDKs are optional.** In Python they're extras (`pip install "cendor-sdk[anthropic]"`); in
  TypeScript they're peer deps (`npm i @anthropic-ai/sdk`). Install only the ones you call — Cendor
  never pulls a provider SDK for you.
- **Everything is offline by default.** Token counting, pricing, guardrails, and cassette replay need
  no network and no API key. Nothing phones home.
- **Python is a PEP 420 namespace.** Import from the flat `cendor.*` path (`from cendor.tokenguard
  import budget`). There is no top-level `cendor` module object to import — install the package that
  owns the symbol (or the `cendor-libs` umbrella).

## Canonical examples

These are the exact snippets from [Getting Started](getting-started.md), reproduced here so an
assistant has the whole happy path in one place. They are typechecked in CI, so they can't drift.

### Instrument once

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from cendor.core import instrument
client = instrument(OpenAI())   # OpenAI · Anthropic · Hugging Face · Gemini · Bedrock · Ollama
```

<!-- tab: TypeScript -->

```ts
import { instrument } from '@cendor/core';
const client = instrument(new OpenAI());   // OpenAI · Anthropic · Hugging Face · Gemini · Bedrock · Ollama
```

<!-- /tabs -->

`instrument()` is idempotent (re-wrapping is a no-op), additive (coexists with other
instrumentation), and supports sync, async, **and streaming** clients.

### Count tokens and estimate cost, offline

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from cendor.core import tokens, prices

n = tokens.count([{"role": "user", "content": "Summarize this in 3 bullets."}], model="claude-opus-4-8")
cost = prices.estimate("claude-opus-4-8", input_tokens=n, output_tokens=200)
print(n, cost)            # e.g. 13  0.005065 USD
```

<!-- tab: TypeScript -->

```ts
import { tokens, prices } from '@cendor/core';

const n = tokens.count([{ role: 'user', content: 'Summarize this in 3 bullets.' }], 'claude-opus-4-8');
const cost = prices.estimate('claude-opus-4-8', n, { outputTokens: 200 });
console.log(n, cost.toString());   // e.g. 13  0.005065 USD
```

<!-- /tabs -->

### Cap spend and attribute it

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from cendor.core import instrument
from cendor.tokenguard import budget, track, report

client = instrument(OpenAI())

@budget(usd=0.50, on_exceed="raise")          # trips the breaker before a runaway loop spends more
def answer(q: str) -> str:
    with track(feature="support", user_id="alice"):
        r = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": q}])
        return r.choices[0].message.content

answer("Why was I charged twice?")
print(report(group_by=["feature"]))            # spend grouped by tag — for free
```

<!-- tab: TypeScript -->

```ts
import { instrument } from '@cendor/core';
import { budget, track, report } from '@cendor/tokenguard';

const client = instrument(new OpenAI());

const answer = budget({ usd: 0.50, onExceed: 'raise' })(   // trips before a runaway loop spends more
  (q: string) => track({ feature: 'support', userId: 'alice' }, async () => {
    const r = await client.chat.completions.create({
      model: 'gpt-4o', messages: [{ role: 'user', content: q }] });
    return r.choices[0].message.content;
  }));

await answer('Why was I charged twice?');
console.log(report(['feature']));              // spend grouped by tag — for free
```

<!-- /tabs -->

### Assemble context within a budget

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from cendor.contextkit import Context, Block

ctx = Context(budget_tokens=8000, model="gpt-4o", reserve_output=1000)
ctx.add(Block(SYSTEM_PROMPT, priority=10, pin=True, role="system"))
ctx.add(Block(retrieved_docs, priority=5, evict="compress"))   # squeeze, if installed
ctx.add(Block(user_msg, priority=9, pin=True, role="user"))

messages = ctx.assemble()      # guaranteed within budget
print(ctx.report())            # the receipt: kept / truncated / dropped
```

<!-- tab: TypeScript -->

```ts
import { Context, Block } from '@cendor/contextkit';

const ctx = new Context({ budgetTokens: 8000, model: 'gpt-4o', reserveOutput: 1000 });
ctx.add(new Block(SYSTEM_PROMPT, { priority: 10, pin: true, role: 'system' }));
ctx.add(new Block(retrievedDocs, { priority: 5, evict: 'compress' }));  // @cendor/squeeze, if installed
ctx.add(new Block(userMsg, { priority: 9, pin: true, role: 'user' }));

const messages = await ctx.assemble();  // guaranteed within budget
console.log(ctx.report());              // the receipt: kept / truncated / dropped
```

<!-- /tabs -->

### Gate unsafe input and output

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from cendor.guardrails import rules, evaluate, GuardrailTripped

gate = [rules.keyword_deny(["ignore previous instructions"], action="block")]
try:
    payload, decisions = evaluate(gate, "input", user_msg)   # runs the input-stage rules
    resp = client.chat.completions.create(model="gpt-4o", messages=messages)
except GuardrailTripped as trip:
    resp = None                                              # blocked pre-flight — $0
    print("blocked:", [d.guardrail for d in trip.decisions])
```

<!-- tab: TypeScript -->

```ts
import { rules, evaluate, GuardrailTripped } from '@cendor/guardrails';

const gate = [rules.keywordDeny(['ignore previous instructions'], { action: 'block' })];
try {
  const { payload } = evaluate(gate, 'input', userMsg);    // runs the input-stage rules
  const resp = await client.chat.completions.create({ model: 'gpt-4o', messages });
  console.log(payload, resp);
} catch (trip) {
  if (trip instanceof GuardrailTripped) {                  // blocked pre-flight — $0
    console.log('blocked:', trip.decisions.map((d) => d.guardrail));
  }
}
```

<!-- /tabs -->

Under the [`cendor-sdk`](/docs/sdk/guardrails) agent loop you don't call `evaluate` yourself — you
pass `Agent(guardrails=[…])` and it gates all four stages for you.

### Make runs testable — and audited

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from cendor import cassette
from cendor.acttrace import AuditLog

audit = AuditLog(system="support", risk_tier="limited")   # auto-logs every instrumented call

@cassette.use("tests/support.json")          # records once, then replays offline forever — no key
def test_support():
    out = answer("Why was I charged twice?")
    assert cassette.semantic_match(out, "explains the charge")

audit.export("evidence.jsonl", framework="eu_ai_act")     # tamper-evident; verify offline
```

<!-- tab: TypeScript -->

```ts
import * as cassette from '@cendor/cassette';
import { AuditLog } from '@cendor/acttrace';

const audit = new AuditLog('support', { riskTier: 'limited' });  // auto-logs every instrumented call

test('support', () =>
  cassette.using('tests/support.json', async () => {  // records once, then replays offline — no key
    const out = await answer('Why was I charged twice?');
    expect(cassette.semanticMatch(out, 'explains the charge')).toBe(true);
  }));

audit.export('evidence.jsonl', 'eu_ai_act');           // tamper-evident; verify offline
```

<!-- /tabs -->

## Wire up your assistant — three ways

You don't have to paste this page every time. Pick whichever fits how your assistant reads context —
they stack, so use more than one:

- **Rules files** — drop a short cheatsheet into your repo (`.github/copilot-instructions.md`,
  `.cursor/rules/cendor.mdc`, `AGENTS.md`, `CLAUDE.md`, or `.windsurf/rules`) so your assistant reads
  the correct call-shapes on every edit. The copy-paste blocks are on **[Rules files](assistant-rules.md)**.
- **MCP server** — if your assistant runs in **agent mode** (Claude Code, Cursor's agent, Copilot
  agent, Windsurf Cascade), connect the read-only **Cendor MCP server** and it *looks up* the correct
  shape on demand — the same trap table above, served fresh. See **[MCP server](assistant-mcp.md)**.
- **One command** — `npx @cendor/init` (or `uvx cendor-init`) writes the rules files (and, with
  `--mcp`, the connect config) for you, idempotently. It also ships a `doctor` that static-checks your
  wiring for CI. See **[init CLI & doctor](assistant-init.md)**.

> These rules files are a *different artifact* from Cendor's own maintainer `CLAUDE.md` (which says
> things like "never create `__init__.py`"). Don't copy that one — it's about developing Cendor, not
> calling it.

## Honest limits

- This page states call **shapes**, never performance numbers. Every benchmark-backed claim lives in
  [Benchmarks](benchmarks.md); `acttrace` produces *evidence*, not a compliance guarantee.
- The type-level teaching is only as fresh as your installed version. If a shape here disagrees with
  what your editor shows on hover, trust the editor — it's reading the version you actually have.
- Parity is documented, not version-coupled. Where a capability is Python-only (or shapes differ),
  the [Languages & parity](languages.md) matrix is the source of truth.
