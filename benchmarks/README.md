# Benchmarks

Reproducible, **offline** benchmarks for the Cendor stack — measuring both the headline
**effectiveness** claims (compression ratios, token-count accuracy, tamper detection, reversibility)
and the **runtime cost** (throughput and per-call overhead) of each package.

No network and no API keys are involved: model calls use fake, provider-shaped clients, timing is
plain `time.perf_counter`, and sample data is generated deterministically. The rendered results live
in [`docs/benchmarks.md`](../docs/benchmarks.md).

## Run

```bash
uv run python benchmarks/run_all.py            # print every table to the console
uv run python benchmarks/run_all.py --write    # also regenerate docs/benchmarks.md

# Token-accuracy rows compare the offline heuristic to the real OpenAI tokenizer:
uv run --with tiktoken python benchmarks/run_all.py --write
```

Run a single package's benchmark directly:

```bash
uv run python benchmarks/bench_squeeze.py
```

## Layout

| File | Measures |
|---|---|
| `_harness.py` | timing (`timed`/`rate`), formatting, bus/interceptor isolation, environment header |
| `_data.py` | deterministic sample data (verbose JSON, noisy logs, code, prose, chat) |
| `bench_core_tokens.py` | token-count accuracy vs tiktoken + counting throughput |
| `bench_squeeze.py` | compression ratio per kind, reversibility, MB/s |
| `bench_contextkit.py` | budget utilization, overflow safety, assemble() latency, determinism |
| `bench_tokenguard.py` | `instrument()` overhead, tokenguard's added per-call cost, `report()` speed, bus dispatch |
| `bench_cassette.py` | replay vs live, per-call replay overhead, `semantic_match` |
| `bench_acttrace.py` | append/verify throughput, HMAC signing cost, tamper detection |
| `run_all.py` | runs everything, prints a summary, and writes `docs/benchmarks.md` |

## Method & caveats

- **Fake clients.** Each "LLM call" is a fake object matching the provider's shape (`chat.completions.create`, …) with synthetic-but-realistic usage. The stack rides one `instrument()` seam, so this exercises the real code paths without a network.
- **Token accuracy** uses `tiktoken` as ground truth for the OpenAI family — core's counts are exact (0% error) **by default** (`tiktoken` is a required dependency); the heuristic is only a defensive fallback if it fails to import. Claude/Gemini have no offline native tokenizer, so that row is a cross-tokenizer ballpark only.
- **Cassette speedup** models a real call with a few-millisecond sleep; production LLM calls are far slower, so the real speedup is much larger than reported.
- **Throughput** numbers are single-machine and hardware-dependent — treat them as relative, and re-run locally for your own figures.

Each `bench_*.py` exposes `run() -> list[Result]`; adding a benchmark is a new module plus an entry
in `run_all.py`.
