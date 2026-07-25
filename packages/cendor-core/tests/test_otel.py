"""otel.span is a no-op (yields None) when OpenTelemetry isn't installed — never raises."""

from cendor.core import otel


def test_span_is_noop_without_otel(no_otel):
    # OTel is an optional extra; the `no_otel` fixture makes the import fail, which is the posture
    # most users run in (it IS installed in this workspace's test env — see the workspace conftest).
    with otel.span("gpt-4o", provider="openai", custom="x") as s:
        assert s is None


def test_span_yields_a_span_when_a_provider_is_configured(otel_traces):
    with otel.span("gpt-4o", provider="openai", custom="x") as s:
        assert s is not None
    spans = otel_traces.get_finished_spans()
    assert [sp.name for sp in spans] == ["chat gpt-4o"]
    assert spans[0].attributes["gen_ai.request.model"] == "gpt-4o"
    assert spans[0].attributes["custom"] == "x"
