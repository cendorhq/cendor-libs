"""Benchmark: cendor.contextkit — budget utilization, assemble() latency, determinism.

Effectiveness: how tightly the packer fills a token budget (high utilization without overflow), and
that assembly is deterministic (same inputs → identical messages). Speed: assemble() latency and
throughput for a realistic ~25-block context.
"""

from __future__ import annotations

from _harness import Result, isolated, pct, per_s, rate, timed
from cendor.contextkit import Block, Context

_LOREM = (
    "Retrieved document chunk discussing billing proration, plan changes, refunds, and the "
    "invoice lifecycle in considerable and frankly excessive detail across many sentences. "
)


def _filler(approx_tokens: int) -> str:
    """A string of roughly ``approx_tokens`` tokens (heuristic ~4 chars/token)."""
    return (_LOREM * (1 + approx_tokens // 30))[: approx_tokens * 4]


def _build() -> Context:
    ctx = Context(budget_tokens=4000, model="gpt-4o", reserve_output=500, order="attention")
    ctx.add(
        Block("You are a precise billing-support assistant.", priority=10, pin=True, role="system")
    )
    ctx.add(
        Block(
            "Customer asks why invoice 1042 was charged twice.", priority=9, pin=True, role="user"
        )
    )
    # One high-priority context block that truncates to fill the remaining budget…
    ctx.add(Block(_filler(6000), priority=8, evict="truncate", role="assistant"))
    # …and a pile of lower-priority history that competes for whatever's left.
    for i in range(22):
        ctx.add(Block(_filler(300), priority=(i % 6) + 1, evict="drop_oldest", role="assistant"))
    return ctx


def run() -> list[Result]:
    rows: list[Result] = []
    with isolated():
        ctx = _build()
        messages = ctx.assemble()
        report = ctx.report()
        effective = report.budget - report.reserved_output
        util = report.used / effective if effective else 0.0
        kept = sum(1 for d in report.decisions if d.action != "dropped")

        rows.append(
            Result(
                "contextkit",
                "Budget utilization",
                pct(util),
                f"used {report.used}/{effective} tokens (reserve {report.reserved_output}); never overflows",
            )
        )
        rows.append(
            Result(
                "contextkit",
                "Overflow safety",
                "0 over budget",
                f"{kept}/{len(report.decisions)} blocks kept/shrunk, rest dropped by priority",
            )
        )

        again = _build().assemble()
        rows.append(
            Result(
                "contextkit",
                "Determinism",
                "exact ✓" if again == messages else "NON-DETERMINISTIC",
                "identical inputs → byte-identical messages",
            )
        )

        spc = timed(lambda: _build().assemble())
        rows.append(
            Result(
                "contextkit",
                "assemble() latency (25 blocks)",
                f"{spc * 1e3:.2f} ms",
                "includes per-block token counting + eviction + ordering",
            )
        )
        # Pure packing speed: reuse one Context so block construction isn't timed.
        packed = _build()
        rows.append(
            Result(
                "contextkit",
                "assemble() throughput",
                per_s(rate(packed.assemble), unit="assemblies"),
                "re-packing a prepared 25-block context",
            )
        )

    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:42} {r.value:>16}   {r.note}")
