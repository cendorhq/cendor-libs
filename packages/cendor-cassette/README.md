# cendor-cassette

Record an agent run once; replay it forever — deterministic, offline, and free. Unlike `vcrpy`
(HTTP-only), it captures the *whole* run: every LLM call and tool call, in order.

**Agent tests with no API key — a recorded run replays in microseconds per call, offline.**

![PyPI](https://img.shields.io/pypi/v/cendor-cassette) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-cassette`

```python
from cendor.core import instrument
from cendor import cassette

client = instrument(OpenAI())          # the same instrumented seam used in production

@cassette.use("triage_happy_path.json")   # record first run, replay after (auto mode)
def test_triage():
    result = my_agent.run("My card was charged twice")
    assert "refund" in result.tools_called
    assert cassette.semantic_match(result.answer, "offers a refund")
```

## Highlights

- **Whole-run capture** — every LLM **and** tool call, in order (not just HTTP, like `vcrpy`).
- **Four modes** — `auto` (record then replay) · `record` · `replay` (fail on an unrecorded call) · **`rerecord`** (run live, report `drift()` without overwriting the committed cassette).
- **Decorator or context manager** — `@cassette.use("run.json")` / `with cassette.using(...)` (handy in pytest fixtures).
- **Meaning-based assertions** — `semantic_match(actual, expected)` (offline lexical default; opt into a free **offline local-embedding** scorer, a BYO-provider embedder, or an LLM judge). `semantic_drift()` filters `rerecord` noise down to real regressions.
- **Pluggable matching + redaction** — a `normalizer` ignores volatile fields; secrets/PII redacted on write, but matching hashes the **un-redacted** request so redaction never collapses two distinct calls (`redact=True|False|callable`).
- **Parallel-safe** — recording is scoped to the active `using()`/`use()` context (a `ContextVar`), so concurrent blocks never capture each other's calls; cassettes are written atomically. Under **pytest-xdist**, give each worker its own cassette path (e.g. suffix with `PYTEST_XDIST_WORKER`) so workers don't race on one file.
- **Faithful replay** — dict-response providers (Ollama/Bedrock) replay as dicts and SDK-object providers as attribute objects; `stream=True` and `stream=False` calls match their own recordings (cassette format **v2**; committed v1 cassettes still replay).
- **`promote()`** turns a production JSONL trace into a replayable regression test (LLM **and** tool calls).

## Semantic matching (opt-in)

`semantic_match` defaults to `lexical_score` — offline, deterministic, zero-dependency. For
meaning-aware (negation-sensitive) checks, pass a `scorer` into the existing hook. cassette binds no
model and adds no dependency unless you ask for one. Four tiers, hermetic-and-free → meaning-aware-but-costly:

1. **Lexical** (default) — `lexical_score`. Hermetic, deterministic, free, zero-dep.
2. **Local embeddings** (recommended) — `local_embedding_scorer()`, free/offline/deterministic via
   [model2vec](https://github.com/MinishLab/model2vec) static embeddings (numpy-only, **no torch**, ~8–30 MB).
   Behind `pip install 'cendor-cassette[embeddings]'`.
3. **BYO provider embeddings** — `embedding_scorer(embed_fn)` wraps any provider (OpenAI
   `text-embedding-3-small/large`, Google `gemini-embedding`, Cohere `embed-v3`; **Anthropic has no
   embeddings API → use Voyage**). Non-hermetic: a cloud embedder calls the network at score time.
   `openai_embedding_scorer(client, model="text-embedding-3-small")` is a thin convenience over an
   already-built OpenAI-shaped client.
4. **LLM-judge** — a `scorer` that calls your own instrumented client (a documented recipe, never a
   shipped dependency). Non-hermetic, non-deterministic, costs money.

```python
from cendor import cassette

score = cassette.local_embedding_scorer()                 # free, offline, deterministic
assert cassette.semantic_match(result.answer, "offers a refund", scorer=score)
assert not cassette.semantic_match("we will not offer a refund", "offers a refund", scorer=score)
```

`drift()` stays byte-exact; at temperature > 0 it flags every run. `semantic_drift(threshold=0.8,
scorer=None)` re-scores each divergence's recorded-vs-live text and keeps only those below the
threshold (real regressions, with a `score`), so cosmetic rewording is ignored. The alternative for
byte-stable drift: record/replay at `temperature=0`.

**Wrap-around, test-time only** — records via core's bus, replays via a core interceptor; no second patch, no network.

See [`docs/cassette.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/cassette.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-cassette/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
