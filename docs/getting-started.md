# Getting Started

> **Two languages, one API.** Everything on this page works in Python (`cendor.*`) and
> TypeScript (`@cendor/*`) — same names modulo `snake_case` ↔ `camelCase`, same defaults. The
> full split is in [Languages & parity](languages.md).

## 1. Install

<!-- tabs: lang -->
<!-- tab: Python -->

```bash
pip install cendor-libs         # the whole stack (umbrella; `cendor` is an alias for it)
# — or pick à la carte; each pulls cendor-core transitively —
pip install cendor-tokenguard cendor-contextkit
```

Exact token counting ships by default (`tiktoken` is a required dependency of `cendor-core`).
Optional extras (provider SDKs and OpenTelemetry are never required):

```bash
pip install "cendor-core[otel]"            # emit OpenTelemetry gen_ai.* spans
pip install "cendor-contextkit[squeeze]"   # enable Block(evict="compress")
```

<!-- tab: TypeScript -->

```bash
npm i @cendor/libs              # the whole stack (umbrella)
# — or pick à la carte; each pulls @cendor/core transitively —
npm i @cendor/tokenguard @cendor/contextkit
```

ESM-only. Provider SDKs (`openai`, `@anthropic-ai/sdk`) are peer dependencies — install the ones
you call. Token counting uses `js-tiktoken` (bundled), so counts match Python exactly.

<!-- /tabs -->

## 2. The one idea: instrument once

Everything composes because you wrap your provider client **once**. From then on, every sibling
tool observes each call through a shared in-process event bus — no per-call wiring.

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
instrumentation), and supports sync, async, **and streaming** (`stream=True`) clients.

## 3. Try it offline (no API key)

Token counting and pricing ship offline, so this runs with zero network:

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

The price table is offline-first but **refreshable**: `prices.refresh(source="litellm"|"openrouter"|"azure")`
pulls live rates from no-auth sources (no extra deps). `prices.age_days()` / `prices.is_stale()` tell
you when the bundled snapshot is getting old.

## 4. A first real call, with a budget and attribution

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

## 5. Add context assembly

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

## 6. Gate unsafe input and output

`guardrails` is the deterministic **Gate**: keyword / regex / URL / length / JSON-schema rules that
`block`, `redact`, or `flag` at four stages (input, tool call, tool output, output) — offline, in
microseconds, and every decision lands on the audit chain. A `block` raises `GuardrailTripped`
*before* the model runs, so the call never costs anything.

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
pass `Agent(guardrails=[…])` and it gates all four stages in the loop for you.

## 7. Make runs testable — and audited

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

> **Want it all wired together?** The full support agent — budget + context + record/replay + audit
> in one function — is in the [Cookbook](/cookbook).

## Next steps

- See how the pieces connect → [Architecture](architecture.md)
- Use a specific provider (incl. Azure AI Foundry, Gemini, Bedrock, Ollama) → [Providers & Integration](providers.md)
- Per-library manuals → [core](core.md) · [contextkit](contextkit.md) · [squeeze](squeeze.md) · [tokenguard](tokenguard.md) · [guardrails](guardrails.md) · [cassette](cassette.md) · [acttrace](acttrace.md)
