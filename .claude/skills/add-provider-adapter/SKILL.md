---
name: add-provider-adapter
description: Add a new LLM provider (gemini, bedrock, ollama, etc.) to cendor-core's token counting, price table, and instrument() interception. Use when adding provider support to the stack.
---
# Add a provider adapter to cendor-core

For provider `<p>`:

1. **tokens** (`core/tokens.py`): register a tokenizer/counter for `<p>` model families.
2. **prices** (`core/prices.json`): add `<p>` model input/output (and cached) rates to the bundled snapshot.
3. **instrument** (`core/instrument.py`): add a client detector + wrapper that normalizes `<p>` requests/responses into `LLMCall` and emits to the event bus. Support sync and async.
4. Make the provider SDK an **optional extra** in `cendor-core`'s `pyproject.toml` (`[project.optional-dependencies] <p> = ["..."]`) — never a hard dependency.
5. Add golden token + price tests (see **write-tests**).
6. Document the supported `<p>` model ids.

Keep core's public API unchanged where possible — adding a provider should not expand the surface.
