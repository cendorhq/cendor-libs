# Benchmarks

Reproducible, offline measurements of every package — both the **headline claims** (compression
ratios, token-count accuracy, tamper detection) and **runtime cost** (throughput, per-call
overhead). No network, no API key: model calls use fake, provider-shaped clients, and timing is
plain `time.perf_counter`.

## How to reproduce

```bash
uv run python benchmarks/run_all.py                    # all tables below
uv run --with tiktoken python benchmarks/run_all.py    # adds exact-token accuracy
```

## Environment

| | |
|---|---|
| Python | 3.12.10 |
| Platform | Windows-11-10.0.26200-SP0 |
| Processor | Intel64 Family 6 Model 154 Stepping 3, GenuineIntel |
| Token counting | tiktoken (exact OpenAI) |
| Package versions | core 1.0.0, contextkit 1.0.0, squeeze 1.0.0, tokenguard 1.0.0, cassette 1.0.0, acttrace 1.0.0 |
| Generated | 2026-07-03 |

## cendor-core

One `instrument()` seam, provider-aware token counting, and offline pricing — measured for accuracy
against tiktoken and for the per-call overhead the seam adds.

| Metric | Result | Notes |
|---|---|---|
| Offline heuristic error vs tiktoken — prose | **35.8%** | heuristic 163 vs exact 120 tokens |
| Offline heuristic error vs tiktoken — code | **8.4%** | heuristic 103 vs exact 95 tokens |
| Offline heuristic error vs tiktoken — json | **18.4%** | heuristic 62 vs exact 76 tokens |
| Exact mode error (with [tiktoken] extra) | **0.0%** | OpenAI counts are exact when tiktoken is installed |
| Offline subword fallback vs o200k (Claude/Gemini) | **33.2%** | the no-tiktoken path; WITH [tiktoken], Claude/Gemini use o200k directly |
| Counting path with [tiktoken] installed | **OpenAI=exact, Claude=bpe-estimate** | method() picks exact / bpe-estimate automatically; heuristic without the extra |
| tokens.count throughput — OpenAI heuristic | **1.14M ops/s** | on a 1.4 KB string |
| tokens.count throughput — subword estimate | **8.2K ops/s** | on a 1.4 KB string |
| tokens.count throughput — tiktoken exact | **5.4K ops/s** | on a 1.4 KB string |
| instrument() overhead per call | **25.30 µs** | bus emit + usage extraction + Decimal pricing; over a no-op client |
| bus dispatch (3 subscribers) | **1.44M emits/s** | synchronous fan-out to subscribed tools |

## cendor-contextkit

Packing prioritized blocks into a token budget: how tightly it fills the budget, that it never
overflows, and how fast it assembles.

| Metric | Result | Notes |
|---|---|---|
| Budget utilization | **100%** | used 3500/3500 tokens (reserve 500); never overflows |
| Overflow safety | **0 over budget** | 3/25 blocks kept/shrunk, rest dropped by priority |
| Determinism | **exact ✓** | identical inputs → byte-identical messages |
| assemble() latency (25 blocks) | **31.86 ms** | includes per-block token counting + eviction + ordering |
| assemble() throughput | **31 assemblies/s** | re-packing a prepared 25-block context |

## cendor-squeeze

Content-aware, reversible compression: how much each kind shrinks (by characters and tokens), that
every compression restores byte-for-byte, and throughput.

| Metric | Result | Notes |
|---|---|---|
| JSON compression | **48.9%** | 90.1 KB → 46.0 KB; 50.1% fewer tokens |
| Logs (repetitive) compression | **99.7%** | 70.1 KB → 0.2 KB; 99.8% fewer tokens |
| Logs (mixed-entropy) compression | **30.1%** | 80.9 KB → 56.5 KB; 35.9% fewer tokens |
| Code compression | **52.5%** | 11.9 KB → 5.7 KB; 42.4% fewer tokens |
| Prose compression | **49.1%** | 8.6 KB → 4.4 KB; 46.6% fewer tokens |
| Reversibility (expand() == original) | **5/5 exact** | every kind restores byte-for-byte from the content-addressed store |
| compress() throughput (JSON) | **34 MB/s** | 90 KB payload, 2.61 ms/call |

## cendor-tokenguard

Budget enforcement + spend attribution as a bus subscriber: the cost it adds per call and how fast
it aggregates spend.

| Metric | Result | Notes |
|---|---|---|
| Added overhead per call (@budget + track) | **5.06 µs** | records spend by tags + checks the active budget(s) |
| report() over 5000 spend rows | **8.52 ms** | group-by aggregation into per-tag cost rows |

## cendor-cassette

Record once, replay forever: a full run replayed vs live, the per-call replay overhead, and
meaning-based matching.

| Metric | Result | Notes |
|---|---|---|
| 25-call run: replayed vs live | **977.42 µs vs 117.19 ms** | live = fake client sleeping 4 ms/call (real LLMs are far slower) |
| Replay speedup | **120×** | at the modeled 4 ms/call; scales with real latency |
| Replay overhead per call | **39.10 µs** | hash the request, look up the recorded response, reconstruct it |
| semantic_match (lexical default) | **✓ accept + reject** | accepts a paraphrase, rejects an unrelated answer |

## Method & caveats

- **No network, no keys.** Every model call is a fake client matching the provider's shape; usage and
  responses are synthetic but realistic.
- **Token accuracy** compares the offline heuristic to `tiktoken` (the real OpenAI tokenizer). With
  `[tiktoken]`, OpenAI counts are exact (0% error); the heuristic is the zero-dependency fallback.
  Claude/Gemini have no offline native tokenizer, so that row is a cross-tokenizer ballpark.
- **Cassette speedup** models a real call with a fake client that sleeps a few milliseconds;
  production LLM calls are 100×–1000× slower, so the real-world speedup is far larger than shown.
- **Compression ratios depend on input shape and repetition.** The log rows report **both** a
  repetition-heavy sample (~55% identical heartbeat lines, as much production traffic is) and a
  mixed-entropy sample (~15% heartbeats). The mixed row is the honest lower bound — read it for less
  repetitive logs; the headline log ratio is repetition-driven.
- Throughput numbers are single-machine and relative; they vary with hardware. Re-run locally for
  your own figures.
