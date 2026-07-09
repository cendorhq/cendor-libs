"""Run the whole Cendor benchmark suite.

    uv run python benchmarks/run_all.py            # print results to the console
    uv run python benchmarks/run_all.py --write    # also (re)generate docs/benchmarks.md
    uv run --with tiktoken python benchmarks/run_all.py --write   # include exact-token accuracy

Every number here is produced offline with fake, provider-shaped clients — no network, no API keys.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import bench_acttrace
import bench_cassette
import bench_contextkit
import bench_core_tokens
import bench_guardrails
import bench_squeeze
import bench_tokenguard
from _harness import Result, environment

# Package display order + the lead-in shown above each table on the docs page.
PACKAGES: list[tuple[str, str, str]] = [
    (
        "core",
        "cendor-core",
        "One `instrument()` seam, provider-aware token counting, and offline pricing — measured for "
        "accuracy against tiktoken and for the per-call overhead the seam adds.",
    ),
    (
        "contextkit",
        "cendor-contextkit",
        "Packing prioritized blocks into a token budget: how tightly it fills the budget, that it "
        "never overflows, and how fast it assembles.",
    ),
    (
        "squeeze",
        "cendor-squeeze",
        "Content-aware, reversible compression: how much each kind shrinks (by characters and tokens), "
        "that every compression restores byte-for-byte, and throughput.",
    ),
    (
        "tokenguard",
        "cendor-tokenguard",
        "Budget enforcement + spend attribution as a bus subscriber: the cost it adds per call and how "
        "fast it aggregates spend.",
    ),
    (
        "guardrails",
        "cendor-guardrails",
        "A deterministic gate at four intervention points: per-check latency for each built-in rule, "
        "the cost of a small pass-through gate, and the per-call overhead the interceptor adds.",
    ),
    (
        "cassette",
        "cendor-cassette",
        "Record once, replay forever: a full run replayed vs live, the per-call replay overhead, and "
        "meaning-based matching.",
    ),
    (
        "acttrace",
        "cendor-acttrace",
        "A tamper-evident hash chain with no server: append/verify throughput, signing cost, and that "
        "a single edited byte is caught.",
    ),
]

_MODULES = [
    bench_core_tokens,
    bench_squeeze,
    bench_contextkit,
    bench_tokenguard,
    bench_guardrails,
    bench_cassette,
    bench_acttrace,
]


def collect() -> list[Result]:
    rows: list[Result] = []
    for mod in _MODULES:
        try:
            rows.extend(mod.run())
        except Exception as exc:  # noqa: BLE001 - one bench failing shouldn't sink the rest
            name = getattr(mod, "__name__", "bench")
            print(f"  ! {name} failed: {exc!r}", file=sys.stderr)
    return rows


def _print_console(rows: list[Result], env: dict[str, str]) -> None:
    print("\nCendor — benchmark results")
    for k, v in env.items():
        print(f"  {k:16} {v}")
    for key, title, _blurb in PACKAGES:
        group = [r for r in rows if r.package == key]
        if not group:
            continue
        print(f"\n{title}")
        for r in group:
            print(f"  {r.metric:48} {r.value:>22}   {r.note}")
    print()


def _to_markdown(rows: list[Result], env: dict[str, str]) -> str:
    out: list[str] = []
    out.append("# Benchmarks\n")
    out.append(
        "Reproducible, offline measurements of every package in the stack — both the **headline "
        "claims** (compression ratios, token-count accuracy, tamper detection) and **runtime cost** "
        "(throughput, per-call overhead). There is no network and no API key anywhere in the suite: "
        "model calls use fake, provider-shaped clients, and timing is plain `time.perf_counter`.\n"
    )
    out.append("## How to reproduce\n")
    out.append(
        "```bash\n"
        "uv run python benchmarks/run_all.py            # all tables below\n"
        "uv run --with tiktoken python benchmarks/run_all.py   # adds exact-token accuracy\n"
        "```\n"
    )
    out.append("## Environment\n")
    out.append("| | |\n|---|---|")
    labels = {
        "python": "Python",
        "platform": "Platform",
        "processor": "Processor",
        "token_counting": "Token counting",
        "versions": "Package versions",
    }
    for k, label in labels.items():
        out.append(f"| {label} | {env.get(k, '?')} |")
    out.append(f"| Generated | {date.today().isoformat()} |\n")

    for key, title, blurb in PACKAGES:
        group = [r for r in rows if r.package == key]
        if not group:
            continue
        out.append(f"## {title}\n")
        out.append(blurb + "\n")
        out.append("| Metric | Result | Notes |")
        out.append("|---|---|---|")
        for r in group:
            note = r.note.replace("|", "\\|")
            out.append(f"| {r.metric} | **{r.value}** | {note} |")
        out.append("")

    out.append("## Method & caveats\n")
    out.append(
        "- **No network, no keys.** Every model call is a fake client matching the provider's shape; "
        "usage and responses are synthetic but realistic.\n"
        "- **Token accuracy** compares the offline heuristic to `tiktoken` (the real OpenAI "
        "tokenizer). With the `[tiktoken]` extra installed, OpenAI counts are exact (0% error); the "
        "heuristic is the zero-dependency fallback. Claude/Gemini have no offline native tokenizer, "
        "so that row is a cross-tokenizer ballpark, not ground truth.\n"
        "- **Cassette speedup** models a real call with a fake client that sleeps a few "
        "milliseconds; production LLM calls are 100×–1000× slower, so the real-world speedup is far "
        "larger than shown here.\n"
        "- **Compression ratios** depend heavily on input shape and repetition (described per row). "
        "The log rows report **both** a repetition-heavy sample (~55% identical heartbeat lines, as "
        "much production log traffic is) and a mixed-entropy sample (~15% heartbeats, the rest "
        "distinct) — the mixed row is the honest lower bound. Inputs are typical verbose payloads, "
        "not adversarially chosen to flatter the compressors, but the headline log ratio is "
        "repetition-driven; read the mixed-entropy row for less repetitive logs.\n"
        "- Throughput numbers are single-machine and relative; they vary with hardware. Re-run "
        "locally for your own figures.\n"
    )
    return "\n".join(out) + "\n"


def main() -> None:
    # The result values use µ, ×, →, ✓ — make console output UTF-8 even on a cp1252 Windows shell
    # (the markdown file is always written UTF-8 regardless).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - older/odd stdouts; printing falls back to replacement
        pass
    rows = collect()
    env = environment()
    _print_console(rows, env)
    if "--write" in sys.argv:
        target = Path(__file__).resolve().parent.parent / "docs" / "benchmarks.md"
        target.write_text(_to_markdown(rows, env), encoding="utf-8")
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
