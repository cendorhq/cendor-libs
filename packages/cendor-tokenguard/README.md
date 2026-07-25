# cendor-tokenguard

Stop runaway LLM bills, and get per-feature / per-user cost attribution for free. One decorator,
one context manager. No dashboard, no account, no infra.

**Cap a runaway loop *before* it overspends — and see which feature or user spent the rest.**

![PyPI](https://img.shields.io/pypi/v/cendor-tokenguard) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-tokenguard`

Using an AI coding assistant? `npx @cendor/init` (TS) / `uvx cendor-init` (Python) wires it up — or point it at [cendor.ai/docs/for-ai-assistants](https://cendor.ai/docs/for-ai-assistants).

```python
from cendor.core import instrument
from cendor.tokenguard import budget, track, report

client = instrument(openai_client)              # wrap once; tokenguard subscribes, never patches

@budget(usd=0.50, on_exceed="downgrade", downgrade={"gpt-4o": "gpt-4o-mini"})
def answer(q: str) -> str:
    with track(feature="support_bot", user_id="alice"):   # ambient attribution, zero bookkeeping
        resp = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": q}])
        return resp.choices[0].message.content

for row in report(group_by=["feature", "user_id"]):       # where did the money go?
    print(row["tags"], row["usd"], row["calls"])
```

## Highlights

- **Pre-flight circuit breaker** — `on_exceed="block"` raises **before** an over-budget call runs; `"downgrade"` reroutes to a cheaper model pre-flight; `"truncate"` degrades; `"raise"` stops a runaway loop; or call your own function.
- **Reasoning models, handled** — you can't predict a thinking model's hidden reasoning pre-flight, so `on_exceed="clamp"` injects the provider's own token ceiling (`max_completion_tokens`/`max_tokens`) sized to the remaining budget — the call is capped *server-side* instead of overspending. `report()` breaks out `reasoning_tokens`, and the cumulative gate enforces on exact usage (which already includes reasoning). See [`docs/tokenguard.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/tokenguard.md) → Reasoning models.
- **Decorator *and* context manager** — budgets **nest** (an inner downgrade never masks an outer hard cap); config is validated at creation (a typo'd `on_exceed` or a map-less `downgrade` is a `ValueError`, never a silent no-op).
- **Cost attribution, free** — `track(feature=…, user_id=…)` tags ambient spend via `contextvars` (sync + async); `report(group_by=[…])` shows where the money went, reasoning tokens included.
- **Cost as a test assertion** — `report().assert_under(usd=0.05, feature="search")`.
- **Pre-flight projection** — `estimate(model, messages)` prices a call *without making it*.
- **Spend reaches your backend on its own** (1.6.0) — with OpenTelemetry installed and a provider configured by your app, every priced row is also written to `gen_ai.client.token.usage` / `.cost.usd` counters through an **internal additive tap**, dimensioned by `model` + your `track(...)` tags. No `use_sink` line needed; `CENDOR_TELEMETRY=off` disables it. Your sink slot stays yours — the tap never displaces it, and it stands down if your own sink already *is* an `OTelSink`.
- **Durable + bounded** — pluggable `use_sink(tokenguard.sinks.SQLiteSink / OTelSink)`; FIFO-bounded in-memory buffer (`configure(max_records=…)`, `dropped()`). For long runs, wrap a durable sink in `sinks.QueueSink(SQLiteSink(path))` so its I/O runs on a background thread and **adds no per-call latency** — `flush()`/`close()` guarantee durability at shutdown.
- **No silent USD blind spots** — a call whose model isn't in the price table records `$0`, so a **USD** cap can't bite. tokenguard warns once per model (`UnpricedModelWarning`) and counts these in `unpriced_calls()` / `report()`'s `unpriced_calls`; `configure(on_unpriced="raise")` makes `on_exceed="block"` reject them. A **token** cap is unaffected — tokens are counted regardless of price.
- **Thread-safe, with one caveat** — the spend buffer and `SQLiteSink` are lock-guarded for concurrent emits, but budgets/tags are `ContextVar`-based: `asyncio` tasks inherit them, a plain `threading.Thread` does **not** (carry them with `contextvars.copy_context()`).

**Streaming timing** — post-flight `raise`/`truncate` fire when a stream is **consumed**, not when it's launched (the call is accounted once the chunk iterator drains). A loop that launches many streams before draining them can overspend — drain each stream before the next, or use a **pre-flight** mode (`block`/`downgrade`/`clamp`), which is unaffected.

**Wrap-around** — it rides the call you already make. Offline and standalone — bundled prices, no account.

See [`docs/tokenguard.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/tokenguard.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-tokenguard/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
