"""The G20 live-spans latch is context-local — the Python pin for the parity fix in @cendor/core.

Python has been correct here since the emitter shipped (the depth is a ``ContextVar``), while
the TS port used a module-global counter until W0.5: one open ``liveSpans`` scope suppressed the
emitter for every concurrent async context in the process, and an unclosed handle stuck it
forever. These tests pin the behaviour the two ports must share, so the asymmetry cannot come
back on either side.

``use_span_emitter(tracer)`` takes an explicit tracer, so nothing here needs OTel installed.
"""

from __future__ import annotations

import asyncio

import pytest
from cendor.core import bus, otel
from cendor.core.types import LLMCall, Usage


class _Rec:
    """A minimal tracer: records the name of every span the emitter starts."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def start_span(self, name, start_time=None):  # noqa: ANN001, ANN201
        self.names.append(name)
        return _Span()


class _Span:
    def set_attribute(self, key, value) -> None:  # noqa: ANN001
        pass

    def end(self, end_time=None) -> None:  # noqa: ANN001
        pass


def _call(model: str) -> LLMCall:
    return LLMCall(id=model, provider="openai", model=model, messages=[], usage=Usage(1, 1))


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


async def test_scope_in_one_task_does_not_suppress_a_concurrent_task():
    rec = _Rec()
    off = otel.use_span_emitter(rec)
    gate = asyncio.Event()

    async def flow_a() -> None:
        otel.enter_live_spans()
        await gate.wait()  # hold the scope open while B runs
        bus.emit(_call("inside-scope"))  # the SDK owns this one — the emitter stands down
        otel.exit_live_spans()

    async def flow_b() -> None:
        await asyncio.sleep(0)
        bus.emit(_call("libs-only"))  # never entered a scope → still gets a flat span

    a = asyncio.create_task(flow_a())
    b = asyncio.create_task(flow_b())
    await b
    gate.set()
    await a
    off()
    assert rec.names == ["chat libs-only"]


async def test_an_unclosed_scope_does_not_suppress_a_later_independent_task():
    rec = _Rec()
    off = otel.use_span_emitter(rec)

    async def leaks() -> None:
        otel.enter_live_spans()  # leaked on purpose — no exit_live_spans()
        bus.emit(_call("leaked"))

    async def later() -> None:
        bus.emit(_call("after-leak"))

    await asyncio.create_task(leaks())
    await asyncio.create_task(later())
    off()
    assert rec.names == ["chat after-leak"]


def test_nesting_counts_and_reopens_on_the_last_exit():
    rec = _Rec()
    off = otel.use_span_emitter(rec)
    otel.enter_live_spans()
    otel.enter_live_spans()
    otel.exit_live_spans()
    bus.emit(_call("still-nested"))  # depth 1 — suppressed
    otel.exit_live_spans()
    bus.emit(_call("reopened"))  # depth 0 — emitted
    otel.exit_live_spans()  # an extra close must not drive the depth negative
    bus.emit(_call("after-extra-exit"))
    off()
    assert rec.names == ["chat reopened", "chat after-extra-exit"]
