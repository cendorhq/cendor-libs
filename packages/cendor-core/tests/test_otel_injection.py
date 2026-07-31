"""S8 — the OTel seams accept an injected tracer/meter, and the global default is unchanged.

WHY (filed by the external black-box suite as a product improvement): ``otel.span()`` had no
``tracer=``, and ``OTelSink`` / the guardrails decisions counter had no ``meter=``, so the ONLY way
to observe any of them was to install a **process-global** provider. cendor-testsuits' keyless tree
had to do exactly that (``localtrace.install_global_tracer`` / ``install_meter``) for these three
APIs and only these three.

Every "now injectable" assertion below is paired with the control that matters more: **the default
still goes to the global provider**, unchanged. An injection seam that quietly stopped honouring the
global provider would break every existing deployment.
"""

from __future__ import annotations

from typing import Any

import pytest


def _in_memory_tracer() -> tuple[Any, Any]:
    """A real SDK tracer whose spans land in an in-memory exporter, with NO global registration."""
    trace_sdk = pytest.importorskip("opentelemetry.sdk.trace")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    inmem = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    exporter = inmem.InMemorySpanExporter()
    provider = trace_sdk.TracerProvider()
    provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _in_memory_meter() -> tuple[Any, Any]:
    """A real SDK meter with an in-memory reader, with NO global registration."""
    metrics_sdk = pytest.importorskip("opentelemetry.sdk.metrics")
    reader_mod = pytest.importorskip("opentelemetry.sdk.metrics.export")
    reader = reader_mod.InMemoryMetricReader()
    provider = metrics_sdk.MeterProvider(metric_readers=[reader])
    return provider.get_meter("test"), reader


# -------------------------------------------------------------- core: otel.span(tracer=)


def test_span_emits_on_an_injected_tracer() -> None:
    from cendor.core import otel

    tracer, exporter = _in_memory_tracer()
    with otel.span("gpt-4o", provider="openai", tracer=tracer, extra="x") as sp:
        assert sp is not None

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["chat gpt-4o"]
    attrs = dict(spans[0].attributes or {})
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["extra"] == "x"


def test_an_injected_tracer_does_not_leak_into_a_second_one() -> None:
    """NEGATIVE CONTROL: two isolated tracers must not see each other's spans."""
    from cendor.core import otel

    tracer_a, exporter_a = _in_memory_tracer()
    tracer_b, exporter_b = _in_memory_tracer()
    with otel.span("model-a", tracer=tracer_a):
        pass
    assert [s.name for s in exporter_a.get_finished_spans()] == ["chat model-a"]
    assert exporter_b.get_finished_spans() == ()


def test_the_default_still_uses_the_global_provider() -> None:
    """NEGATIVE CONTROL: omitting ``tracer=`` must behave exactly as before — global tracer."""
    from cendor.core import otel

    seen: list[str] = []

    class _Recorder:
        def start_as_current_span(self, name: str):  # noqa: ANN202
            seen.append(name)
            from contextlib import nullcontext

            return nullcontext(_NoopSpan())

    class _NoopSpan:
        def set_attribute(self, *_a: Any) -> None: ...

    import opentelemetry.trace as trace_api

    original = trace_api.get_tracer
    try:
        trace_api.get_tracer = lambda name, *a, **k: _Recorder()  # type: ignore[assignment]
        with otel.span("gpt-4o"):
            pass
    finally:
        trace_api.get_tracer = original  # type: ignore[assignment]

    assert seen == ["chat gpt-4o"], "the default path must go through trace.get_tracer"


def test_tracer_none_is_the_default_not_a_disabled_span() -> None:
    """``tracer=None`` means "use the global one", never "emit nothing" — it is the documented
    default."""
    import opentelemetry.trace as trace_api
    from cendor.core import otel

    seen: list[str] = []

    class _Recorder:
        def start_as_current_span(self, name: str):  # noqa: ANN202
            seen.append(name)
            from contextlib import nullcontext

            return nullcontext(type("S", (), {"set_attribute": lambda *_a: None})())

    original = trace_api.get_tracer
    try:
        trace_api.get_tracer = lambda name, *a, **k: _Recorder()  # type: ignore[assignment]
        with otel.span("m", tracer=None):
            pass
    finally:
        trace_api.get_tracer = original  # type: ignore[assignment]
    assert seen == ["chat m"]


# ------------------------------------------------------------------ tokenguard: OTelSink(meter=)


def _spend_entry() -> dict:
    return {
        "model": "gpt-4o",
        "input_tokens": 100,
        "output_tokens": 40,
        "reasoning_tokens": 8,
        "usd": 0.002,
        "tags": {"feature": "search"},
    }


def _counter_totals(reader: Any) -> dict[str, float]:
    data = reader.get_metrics_data()
    totals: dict[str, float] = {}
    if data is None:  # a reader that saw nothing answers None, not an empty tree
        return totals
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    totals[metric.name] = totals.get(metric.name, 0) + point.value
    return totals


def test_otel_sink_writes_to_an_injected_meter() -> None:
    from cendor.tokenguard.sinks import OTelSink

    meter, reader = _in_memory_meter()
    OTelSink(meter=meter).write(_spend_entry())

    totals = _counter_totals(reader)
    assert totals["gen_ai.client.token.usage"] == 140  # 100 + 40
    assert totals["gen_ai.client.reasoning.token.usage"] == 8
    assert totals["gen_ai.client.cost.usd"] == pytest.approx(0.002)


def test_two_injected_meters_stay_isolated() -> None:
    """NEGATIVE CONTROL: a sink on meter A must not increment meter B."""
    from cendor.tokenguard.sinks import OTelSink

    meter_a, reader_a = _in_memory_meter()
    _meter_b, reader_b = _in_memory_meter()
    OTelSink(meter=meter_a).write(_spend_entry())
    assert _counter_totals(reader_a)["gen_ai.client.token.usage"] == 140
    assert "gen_ai.client.token.usage" not in _counter_totals(reader_b)


def test_otel_sink_default_still_uses_the_global_meter() -> None:
    """NEGATIVE CONTROL: no ``meter=`` must still go through metrics.get_meter, as before."""
    from cendor.tokenguard.sinks import OTelSink

    metrics_api = pytest.importorskip("opentelemetry.metrics")
    asked: list[str] = []

    class _Meter:
        def create_counter(self, name: str) -> Any:
            return type("C", (), {"add": lambda self, v, a=None: None})()

    original = metrics_api.get_meter
    try:

        def _get_meter(name: str, *a: Any, **k: Any) -> Any:
            asked.append(name)
            return _Meter()

        metrics_api.get_meter = _get_meter  # type: ignore[assignment]
        OTelSink()
    finally:
        metrics_api.get_meter = original  # type: ignore[assignment]
    assert asked == ["cendor.tokenguard"]


def test_otel_sink_tags_false_still_honoured_with_an_injected_meter() -> None:
    """The injection must not change the other option's behaviour."""
    from cendor.tokenguard.sinks import OTelSink

    meter, reader = _in_memory_meter()
    OTelSink(tags=False, meter=meter).write(_spend_entry())
    data = reader.get_metrics_data()
    assert data is not None
    attr_keys: set[str] = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    attr_keys |= set(dict(point.attributes or {}))
    assert attr_keys == {"model"}, f"tags=False must emit model only, got {attr_keys}"


# --------------------------------------------------------- guardrails: use_meter(meter)


def test_guardrails_decisions_counter_on_an_injected_meter() -> None:
    from cendor import guardrails
    from cendor.guardrails import Context, Verdict, guardrail

    meter, reader = _in_memory_meter()
    guardrails.use_meter(meter)
    try:

        @guardrail(stage="input", name="ban")
        def ban(payload: Any, ctx: Context) -> Verdict | None:
            return Verdict(action="flag", reason="nope")

        guardrails.apply([ban], "input", "anything", Context(stage="input"))
        totals = _counter_totals(reader)
        assert totals["cendor.guardrails.decisions"] == 1
    finally:
        guardrails.use_meter(None)


def test_guardrails_use_meter_none_restores_the_global_default() -> None:
    """NEGATIVE CONTROL: resetting must re-read the global provider, not stay silent forever."""
    from cendor import guardrails
    from cendor.guardrails import Context, Verdict, guardrail

    meter, reader = _in_memory_meter()
    guardrails.use_meter(meter)
    guardrails.use_meter(None)  # reset

    @guardrail(stage="input", name="ban2")
    def ban(payload: Any, ctx: Context) -> Verdict | None:
        return Verdict(action="flag", reason="nope")

    guardrails.apply([ban], "input", "anything", Context(stage="input"))
    # The injected meter must NOT have been used after the reset.
    assert "cendor.guardrails.decisions" not in _counter_totals(reader)


def test_guardrails_counter_never_gates_the_decision() -> None:
    """A meter that raises must not change the verdict — the counter is observability, not a gate.

    This assertion FAILED when it was first written (2026-07-31): the counter's ``add`` was not
    guarded, so a broken metrics backend propagated out of ``apply()`` and took the governance
    decision with it. The docstring already promised "best-effort … never gates the decision"; the
    code did not. Fixed in the same wave, in both languages.
    """
    from cendor import guardrails
    from cendor.guardrails import Context, Verdict, guardrail

    class _Exploding:
        def create_counter(self, name: str) -> Any:
            class _C:
                def add(self, *_a: Any, **_k: Any) -> None:
                    raise RuntimeError("metrics backend down")

            return _C()

    guardrails.use_meter(_Exploding())
    try:

        @guardrail(stage="input", name="ban3")
        def ban(payload: Any, ctx: Context) -> Verdict | None:
            return Verdict(action="flag", reason="nope")

        decisions = guardrails.apply([ban], "input", "anything", Context(stage="input"))
    finally:
        guardrails.use_meter(None)

    # The decision was taken and returned in full, despite the metrics backend being broken.
    assert [(d.guardrail, d.action) for d in decisions] == [("ban3", "flag")]
