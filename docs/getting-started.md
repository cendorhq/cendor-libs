# Getting Started

## 1. Install

```bash
pip install cendor              # the whole stack (umbrella)
# — or pick à la carte; each pulls cendor-core transitively —
pip install cendor-tokenguard cendor-contextkit
```

Optional extras (provider SDKs and precise tokenizers are never required):

```bash
pip install "cendor-core[tiktoken]"        # exact OpenAI token counts
pip install "cendor-core[otel]"            # emit OpenTelemetry gen_ai.* spans
pip install "cendor-contextkit[squeeze]"   # enable Block(evict="compress")
```

## 2. The one idea: instrument once

Everything composes because you wrap your provider client **once**. From then on, every sibling
tool observes each call through a shared in-process event bus — no per-call wiring.

```python
from cendor.core import instrument
client = instrument(OpenAI())   # OpenAI · Anthropic · Bedrock · Gemini · Ollama
```

`instrument()` is idempotent (re-wrapping is a no-op), additive (coexists with other
instrumentation), and supports sync, async, **and streaming** (`stream=True`) clients.

## 3. Try it offline (no API key)

Token counting and pricing ship offline, so this runs with zero network:

```python
from cendor.core import tokens, prices

n = tokens.count([{"role": "user", "content": "Summarize this in 3 bullets."}], model="claude-opus-4-8")
cost = prices.estimate("claude-opus-4-8", input_tokens=n, output_tokens=200)
print(n, cost)            # e.g. 13  0.005065 USD
```

The price table is offline-first but **refreshable**: `prices.refresh(source="litellm"|"openrouter"|"azure")`
pulls live rates from no-auth sources (no extra deps). `prices.age_days()` / `prices.is_stale()` tell
you when the bundled snapshot is getting old.

## 4. A first real call, with a budget and attribution

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

## 5. Add context assembly

```python
from cendor.contextkit import Context, Block

ctx = Context(budget_tokens=8000, model="gpt-4o", reserve_output=1000)
ctx.add(Block(SYSTEM_PROMPT, priority=10, pin=True, role="system"))
ctx.add(Block(retrieved_docs, priority=5, evict="compress"))   # squeeze, if installed
ctx.add(Block(user_msg, priority=9, pin=True, role="user"))

messages = ctx.assemble()      # guaranteed within budget
print(ctx.report())            # the receipt: kept / truncated / dropped
```

## 6. Make runs testable — and audited

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

> **Want it all wired together?** The full support agent — budget + context + record/replay + audit
> in one function — is in the [Cookbook](/cookbook).

## Next steps

- See how the pieces connect → [Architecture](architecture.md)
- Use a specific provider (incl. Azure AI Foundry, Gemini, Bedrock, Ollama) → [Providers & Integration](providers.md)
- Per-library manuals → [core](core.md) · [contextkit](contextkit.md) · [squeeze](squeeze.md) · [tokenguard](tokenguard.md) · [cassette](cassette.md) · [acttrace](acttrace.md)
