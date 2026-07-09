# Benchmarks

Reproducible, offline measurements of every package in the stack — both the **headline claims** (compression ratios, token-count accuracy, tamper detection) and **runtime cost** (throughput, per-call overhead). There is no network and no API key anywhere in the suite: model calls use fake, provider-shaped clients, and timing is plain `time.perf_counter`.

## How to reproduce

```bash
uv run python benchmarks/run_all.py            # all tables below
uv run --with tiktoken python benchmarks/run_all.py   # adds exact-token accuracy
```

## Environment

| | |
|---|---|
| Python | 3.12.10 |
| Platform | Windows-11-10.0.26200-SP0 |
| Processor | Intel64 Family 6 Model 154 Stepping 3, GenuineIntel |
| Token counting | tiktoken (exact OpenAI) |
| Package versions | core 1.4.0, contextkit 1.0.1, squeeze 1.0.1, tokenguard 1.1.1, guardrails 1.0.0, cassette 1.0.1, acttrace 1.3.0 |
| Generated | 2026-07-09 |

## cendor-core

One `instrument()` seam, provider-aware token counting, and offline pricing — measured for accuracy against tiktoken and for the per-call overhead the seam adds.

| Metric | Result | Notes |
|---|---|---|
| Offline heuristic error vs tiktoken — prose | **35.8%** | heuristic 163 vs exact 120 tokens |
| Offline heuristic error vs tiktoken — code | **8.4%** | heuristic 103 vs exact 95 tokens |
| Offline heuristic error vs tiktoken — json | **18.4%** | heuristic 62 vs exact 76 tokens |
| Exact mode error (default) | **0.0%** | OpenAI counts are exact out of the box — `tiktoken` is a required dependency |
| Offline subword fallback vs o200k (Claude/Gemini) | **33.2%** | the defensive no-tiktoken fallback; by default Claude/Gemini use o200k directly |
| Counting path (default) | **OpenAI=exact, Claude=bpe-estimate** | method() picks exact / bpe-estimate automatically; heuristic only if tiktoken fails to import |
| tokens.count throughput — OpenAI heuristic | **1.24M ops/s** | on a 1.4 KB string |
| tokens.count throughput — subword estimate | **14.5K ops/s** | on a 1.4 KB string |
| tokens.count throughput — tiktoken exact | **8.8K ops/s** | on a 1.4 KB string |
| instrument() overhead per call | **12.88 µs** | bus emit + usage extraction + Decimal pricing; over a no-op client |
| bus dispatch (3 subscribers) | **1.97M emits/s** | synchronous fan-out to subscribed tools |

## cendor-contextkit

Packing prioritized blocks into a token budget: how tightly it fills the budget, that it never overflows, and how fast it assembles.

| Metric | Result | Notes |
|---|---|---|
| Budget utilization | **100%** | used 3500/3500 tokens (reserve 500); never overflows |
| Overflow safety | **0 over budget** | 3/25 blocks kept/shrunk, rest dropped by priority |
| Determinism | **exact ✓** | identical inputs → byte-identical messages |
| assemble() latency (25 blocks) | **23.31 ms** | includes per-block token counting + eviction + ordering |
| assemble() throughput | **41 assemblies/s** | re-packing a prepared 25-block context |

## cendor-squeeze

Content-aware, reversible compression: how much each kind shrinks (by characters and tokens), that every compression restores byte-for-byte, and throughput.

| Metric | Result | Notes |
|---|---|---|
| JSON compression | **48.9%** | 90.1 KB → 46.0 KB; 50.1% fewer tokens |
| Logs (repetitive) compression | **99.7%** | 70.1 KB → 0.2 KB; 99.8% fewer tokens |
| Logs (mixed-entropy) compression | **30.1%** | 80.9 KB → 56.5 KB; 35.9% fewer tokens |
| Code compression | **52.5%** | 11.9 KB → 5.7 KB; 42.4% fewer tokens |
| Prose compression | **49.1%** | 8.6 KB → 4.4 KB; 46.6% fewer tokens |
| Reversibility (expand() == original) | **5/5 exact** | every kind restores byte-for-byte from the content-addressed store |
| compress() throughput (JSON) | **54 MB/s** | 90 KB payload, 1.64 ms/call |

## cendor-tokenguard

Budget enforcement + spend attribution as a bus subscriber: the cost it adds per call and how fast it aggregates spend.

| Metric | Result | Notes |
|---|---|---|
| Added overhead per call (@budget + track) | **4.99 µs** | records spend by tags + checks the active budget(s) |
| report() over 5000 spend rows | **8.12 ms** | group-by aggregation into per-tag cost rows |

## cendor-guardrails

A deterministic gate at four intervention points: per-check latency for each built-in rule, the cost of a small pass-through gate, and the per-call overhead the interceptor adds.

| Metric | Result | Notes |
|---|---|---|
| keyword_deny check latency | **4.53 µs** | substring scan of the flattened message text |
| regex_rule check latency | **4.61 µs** | one compiled-regex search over the payload |
| url_allowlist check latency | **5.02 µs** | extract URLs + host allowlist match |
| length_bounds check latency (chars) | **732 ns** | len() of the flattened text |
| length_bounds check latency (tokens) | **23.08 µs** | exact token count via cendor.core.tokens (tiktoken) |
| json_schema check latency | **4.32 µs** | json.loads + minimal type/required/properties validation |
| apply() 4-rule input gate (pass-through) | **55.0K calls/s** | four deterministic checks, nothing trips |
| install() interceptor overhead per call | **24.74 µs** | input gate over an instrumented no-op client (bus emit excluded — nothing trips) |

## cendor-cassette

Record once, replay forever: a full run replayed vs live, the per-call replay overhead, and meaning-based matching.

| Metric | Result | Notes |
|---|---|---|
| 25-call run: replayed vs live | **995.34 µs vs 116.77 ms** | live = fake client sleeping 4 ms/call (real LLMs are far slower) |
| Replay speedup | **117×** | at the modeled 4 ms/call; scales with real latency |
| Replay overhead per call | **39.81 µs** | hash the request, look up the recorded response, reconstruct it |
| semantic_match (lexical default) | **✓ accept + reject** | accepts a paraphrase, rejects an unrelated answer |

## cendor-acttrace

A tamper-evident hash chain with no server: append/verify throughput, signing cost, and that a single edited byte is caught.

| Metric | Result | Notes |
|---|---|---|
| Append throughput (in-memory) | **15.0K entries/s** | sha256 chain + default PII redaction per entry |
| HMAC signing overhead | **+4%** | per-entry HMAC-SHA256 on top of the chain hash |
| Append throughput (file-backed) | **3.1K entries/s** | flush + fsync a JSONL line per entry on a kept-open handle |
| verify() throughput | **54.4K entries/s** | re-walks a 2001-entry chain in 36.8 ms |
| Tamper detection | **✓ detected** | one edited byte → chain hash mismatch → verify() returns False |

## PII / secret detection — acttrace catalogue

Detection *quality* for the regex/validator catalogue the guardrails PII bridge (`rules.pii` / `secrets` / `entropy`, bridged from the SDK) leans on: per-group precision/recall on a small **synthetic** corpus, the false-positive rate on look-alikes, and the regex-vs-NER split for free-text names/addresses. **Read the corpus caveat below before quoting any number** — these figures establish the methodology and per-group behaviour of the shipped catalogue; they are not a headline "we catch X% of PII" claim.

| Metric | Result | Notes |
|---|---|---|
| secret: precision / recall | **100% / 100%** | regex catalogue, 7TP 0FP 0FN on the synthetic corpus |
| financial: precision / recall | **100% / 100%** | regex catalogue, 2TP 0FP 0FN on the synthetic corpus |
| gov_id: precision / recall | **100% / 100%** | regex catalogue, 1TP 0FP 0FN on the synthetic corpus |
| pii: precision / recall | **100% / 100%** | regex catalogue, 4TP 0FP 0FN on the synthetic corpus |
| special_category: precision / recall | **100% / 100%** | regex catalogue, 1TP 0FP 0FN on the synthetic corpus |
| false positives on clean look-alikes | **0/7 lines** | non-Luhn digit runs, partial IPs, prose — validators keep these from tripping |
| overall (structured): precision / recall | **100% / 100%** | aggregate across 5 groups, 15TP 0FP 0FN |
| free-text names/addresses — regex recall | **0%** | the regex catalogue does not target free-text names/addresses by design |
| free-text names/addresses — +NER (Presidio) | **needs the `[ner]` backend** | measured only when `cendor-acttrace[ner]` is installed |
| scan() latency (mixed line) | **~50 µs** | one pass over the full regex catalogue + validators, counts only |

## Method & caveats

- **No network, no keys.** Every model call is a fake client matching the provider's shape; usage and responses are synthetic but realistic.
- **Token accuracy** compares the offline heuristic to `tiktoken` (the real OpenAI tokenizer). With the `[tiktoken]` extra installed, OpenAI counts are exact (0% error); the heuristic is the zero-dependency fallback. Claude/Gemini have no offline native tokenizer, so that row is a cross-tokenizer ballpark, not ground truth.
- **Cassette speedup** models a real call with a fake client that sleeps a few milliseconds; production LLM calls are 100×–1000× slower, so the real-world speedup is far larger than shown here.
- **Compression ratios** depend heavily on input shape and repetition (described per row). The log rows report **both** a repetition-heavy sample (~55% identical heartbeat lines, as much production log traffic is) and a mixed-entropy sample (~15% heartbeats, the rest distinct) — the mixed row is the honest lower bound. Inputs are typical verbose payloads, not adversarially chosen to flatter the compressors, but the headline log ratio is repetition-driven; read the mixed-entropy row for less repetitive logs.
- Throughput numbers are single-machine and relative; they vary with hardware. Re-run locally for your own figures.
- **PII/secret detection is measured on a small, hand-labelled *synthetic* corpus** (`benchmarks/bench_pii_detectors.py`) — every value is fabricated (test keys, Luhn-valid but non-issued cards, RFC-5737 documentation IPs), modelled on the formats used by public PII corpora (Presidio's generator, Faker, the AWS Comprehend entity list) but scraping none of them, so the suite stays offline. The precision/recall figures establish the **methodology and per-group behaviour of the shipped catalogue**; they are **not** a headline "we catch X% of PII" claim — that needs a larger corpus from a licensed public dataset. The regex catalogue targets *structured* PII (patterns + checksum validators), so its recall on free-text **names/addresses is ~0 by design**; the optional Presidio NER backend (`cendor-acttrace[ner]`) covers those and is measured only when installed.

