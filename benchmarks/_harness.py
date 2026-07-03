"""Tiny, dependency-free benchmark harness for the Cendor suite.

No pytest, no pytest-benchmark, no numpy — just ``time.perf_counter`` and stdlib, so the suite
runs anywhere ``cendor`` installs. Each ``bench_*.py`` module exposes ``run() -> list[Result]``
returning rows that mix *effectiveness* (compression %, accuracy, reversibility) and *speed*
(throughput, per-op latency). ``run_all.py`` collects them and renders ``docs/benchmarks.md``.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from time import perf_counter
from typing import Any


@dataclass
class Result:
    """One benchmark row. ``value`` is preformatted for display; keep numbers human-readable."""

    package: str
    metric: str
    value: str
    note: str = ""


# --------------------------------------------------------------------------- timing


def timed(fn: Callable[[], Any], *, target_seconds: float = 0.30, warmup: int = 3) -> float:
    """Auto-calibrating timer. Returns seconds-per-call (median-ish via a growing batch).

    Runs ``fn`` in doubling batches until a batch takes at least ``target_seconds``, then reports
    that batch's per-call time. Deterministic enough for relative comparisons; not a microbenchmark
    framework. ``fn`` should take no args and do one unit of work.
    """
    for _ in range(warmup):
        fn()
    n = 1
    while True:
        t0 = perf_counter()
        for _ in range(n):
            fn()
        dt = perf_counter() - t0
        if dt >= target_seconds:
            return dt / n
        # grow toward the target without overshooting wildly
        grow = max(2, int(n * target_seconds / dt) + 1) if dt > 0 else n * 4
        n *= min(grow, 8)


def rate(fn: Callable[[], Any], *, target_seconds: float = 0.30) -> float:
    """Calls per second for ``fn``."""
    return 1.0 / timed(fn, target_seconds=target_seconds)


# --------------------------------------------------------------------------- formatting


def human(n: float) -> str:
    """Compact magnitude: 1_234_567 -> '1.23M', 12_345 -> '12.3K', 42 -> '42'."""
    a = abs(n)
    if a >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if a >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if a >= 1_000:
        return f"{n / 1e3:.1f}K"
    if a >= 1:
        return f"{n:.0f}"
    return f"{n:.3g}"


def per_s(ops: float, unit: str = "ops") -> str:
    """'1.23M ops/s' style throughput string."""
    return f"{human(ops)} {unit}/s"


def dur(seconds: float) -> str:
    """Human duration: picks ns / µs / ms / s automatically."""
    if seconds < 1e-6:
        return f"{seconds * 1e9:.0f} ns"
    if seconds < 1e-3:
        return f"{seconds * 1e6:.2f} µs"
    if seconds < 1.0:
        return f"{seconds * 1e3:.2f} ms"
    return f"{seconds:.2f} s"


def pct(x: float, decimals: int = 0) -> str:
    """Fraction -> percent string. pct(0.78) -> '78%'."""
    return f"{x * 100:.{decimals}f}%"


# --------------------------------------------------------------------------- isolation


@contextmanager
def isolated():
    """Run a bench against a clean event bus + interceptor list, then restore the originals.

    Several tools subscribe to ``core.bus`` at import or construction (tokenguard at import,
    acttrace on ``AuditLog(...)``) and cassette registers interceptors. Isolating keeps one bench's
    subscribers from skewing another's timings.
    """
    import importlib

    from cendor.core import bus

    # core/__init__ re-exports `instrument` as a function, shadowing the submodule attribute, so
    # `import cendor.core.instrument` yields the function. Fetch the real module from sys.modules.
    _inst = importlib.import_module("cendor.core.instrument")

    saved_subs = list(bus._subscribers)
    saved_ints = list(_inst._interceptors)
    bus._subscribers.clear()
    _inst._interceptors.clear()
    try:
        yield
    finally:
        bus._subscribers[:] = saved_subs
        _inst._interceptors[:] = saved_ints


# --------------------------------------------------------------------------- environment


_PACKAGES = [
    "cendor-core",
    "cendor-contextkit",
    "cendor-squeeze",
    "cendor-tokenguard",
    "cendor-cassette",
    "cendor-acttrace",
]


def _ver(pkg: str) -> str:
    try:
        return version(pkg)
    except PackageNotFoundError:
        return "?"


def tiktoken_present() -> bool:
    """Whether tiktoken is importable (enables exact OpenAI counts / accuracy ground truth)."""
    try:
        import tiktoken  # noqa: F401

        return True
    except ImportError:
        return False


def environment() -> dict[str, str]:
    """Reproducibility header: interpreter, platform, token mode, and package versions."""
    token_mode = (
        "tiktoken (exact OpenAI)" if tiktoken_present() else "heuristic (offline, no extras)"
    )
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "token_counting": token_mode,
        "versions": ", ".join(f"{p.split('-')[-1]} {_ver(p)}" for p in _PACKAGES),
    }
