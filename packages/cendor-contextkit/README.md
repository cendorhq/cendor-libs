# cendor-contextkit

Treat the context window like a packed suitcase, not a string you concatenate. Declare blocks
with priorities and eviction rules; contextkit fits them to a token budget and tells you exactly
what it kept, shrank, and dropped.

**Every assembled prompt comes with a receipt.**

![PyPI](https://img.shields.io/pypi/v/cendor-contextkit) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-contextkit`

```python
from cendor.contextkit import Context, Block

ctx = Context(budget_tokens=8000, model="claude-opus-4-8", reserve_output=1000, order="attention")
ctx.add(Block(system_prompt, priority=10, pin=True, role="system"))
ctx.add(Block(retrieved_docs, priority=5, evict="compress"))        # uses squeeze if installed
ctx.add(Block(messages=chat_history, priority=3, evict="drop_oldest"))  # peels OLDEST turns
ctx.add(Block(user_msg, priority=9, pin=True, role="user"))

messages = ctx.assemble()      # provider-ready, guaranteed within budget — including framing
print(ctx.report())            # the receipt: kept / truncated / dropped + token math
preview = ctx.whatif(budget_tokens=4000)   # same inputs, tighter budget, no commit
```

## Highlights

- **Token-budgeted packing** — declare `Block`s with `priority` and `pin`; `assemble()` fits them into the budget deterministically. Pinned blocks are never evicted (raises `BudgetError` if they alone overflow).
- **Per-block eviction** — `drop_oldest` · `truncate` (keep head/tail, with a `…[truncated]` marker) · `summarize` (sync, or async via `aassemble()`) · `compress` (via squeeze — **reversible**: the receipt's `BlockDecision.handle.expand()` restores the original, and compression is sized against *your* model) · or any custom `EvictionStrategy`.
- **Real chat-history** — `Block(messages=[…])` peels the *oldest turns* to fit (a sliding window) — never mangling a turn.
- **An honest receipt** — `report().used == tokens.count(assemble(), model)`: packing charges the per-message framing providers add, so a "full" prompt never quietly overflows once sent.
- **Attention-aware ordering** — `order="default"` · `"attention"` (lost-in-the-middle) · `"cache"` (stable prefix for prompt-cache hits).
- **Provider adapters & multimodal** — `for_anthropic()` / `for_gemini()` / `for_bedrock()`; per-image `image_tokens` (int or resolution-aware callable); `whatif(budget)` previews; `use_compressor()` swaps the compression backend.

**Inbound** — call it *before* the model call; `report()` flows onto core's bus, so `acttrace` records what the model actually saw.

See [`docs/contextkit.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/contextkit.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-contextkit/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
