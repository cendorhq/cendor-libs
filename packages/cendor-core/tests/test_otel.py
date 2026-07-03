"""otel.span is a no-op (yields None) when OpenTelemetry isn't installed — never raises."""

from cendor.core import otel


def test_span_is_noop_without_otel():
    # OTel is an optional extra and not installed in the test env.
    with otel.span("gpt-4o", provider="openai", custom="x") as s:
        assert s is None
