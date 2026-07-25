"""Workspace-wide pytest fixtures for the OpenTelemetry paths.

OpenTelemetry is a **test** dependency of this workspace (never a runtime one — it stays
the `cendor-core[otel]` extra), because the telemetry switch and the emitters have to be
observed through a real provider. The OTel API deliberately allows the global provider to be
set only **once** per process, so tests that each need their own in-memory provider must reset
the API's globals; that reset lives here rather than being re-invented per package.

Fixtures:

* ``otel_traces`` — a fresh in-memory tracer provider installed as the global one; yields the
  exporter (``.get_finished_spans()``).
* ``otel_metrics`` — the same for metrics; yields an ``InMemoryMetricReader``.
* ``no_otel`` — makes ``import opentelemetry…`` fail, so the **local-first** rail (OTel absent ⇒
  every emitter is an inert no-op, byte-identical behaviour) stays genuinely pinned even though the
  package is installed here.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pytest


def _reset_otel_globals() -> None:
    """Let a test install its own global providers (the API allows one set() per process)."""
    from opentelemetry import metrics, trace
    from opentelemetry.util._once import Once

    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    metrics._internal._METER_PROVIDER = None
    metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    metrics._internal._METER_PROVIDER_CFG_ONCE = Once()


@pytest.fixture
def otel_traces() -> Iterator[Any]:
    """An in-memory tracer provider registered as the global one. Yields the span exporter."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    _reset_otel_globals()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        provider.shutdown()
        _reset_otel_globals()


@pytest.fixture
def otel_metrics() -> Iterator[Any]:
    """An in-memory meter provider registered as the global one. Yields the metric reader."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    _reset_otel_globals()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    try:
        yield reader
    finally:
        provider.shutdown()
        _reset_otel_globals()


class _BlockOTel:
    """A meta-path finder that makes every ``opentelemetry`` import fail."""

    def find_module(self, fullname: str, path: Any = None) -> Any:  # pragma: no cover - legacy API
        return self.find_spec(fullname, path)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == "opentelemetry" or fullname.startswith("opentelemetry."):
            raise ImportError(f"No module named {fullname!r} (blocked by the no_otel fixture)")
        return None


@pytest.fixture
def no_otel() -> Iterator[None]:
    """Simulate OpenTelemetry not being installed (the default posture for most users)."""
    blocker = _BlockOTel()
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] == "opentelemetry"}
    for k in saved:
        del sys.modules[k]
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved)


@pytest.fixture(autouse=True)
def _reset_cendor_auto_telemetry() -> Iterator[None]:
    """Forget any automatic telemetry subscription between tests (it latches per process)."""
    from cendor.core import otel

    otel._reset_auto_telemetry()
    yield
    otel._reset_auto_telemetry()
