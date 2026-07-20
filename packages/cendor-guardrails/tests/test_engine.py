"""The evaluation engine: apply / evaluate (sync + async), decision emission, stage filtering."""

from __future__ import annotations

import pytest
from cendor.core import trace
from cendor.guardrails import (
    Context,
    GuardrailDecision,
    GuardrailTripped,
    Verdict,
    apply,
    apply_async,
    evaluate,
    evaluate_async,
    guardrail,
    rules,
)


def msgs(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def _flag(name="f", reason="flagged"):
    return rules.custom(lambda p, c: Verdict("flag", reason=reason), stage="input", name=name)


def _block(name="b"):
    return rules.custom(lambda p, c: Verdict("block", reason="no"), stage="input", name=name)


def test_apply_returns_decisions_on_flag(decisions):
    out = apply([_flag()], "input", msgs("hi"))
    assert len(out) == 1 and out[0].action == "flag"
    assert decisions == out  # the same decision reached the bus


def test_apply_raises_on_block():
    with pytest.raises(GuardrailTripped):
        apply([_block()], "input", msgs("hi"))


def test_evaluate_returns_redacted_payload():
    rule = rules.regex_rule(r"secret", action="redact", stage="input")
    payload, decs = evaluate([rule], "input", msgs("a secret value"))
    assert payload[0]["content"] == "a [redacted] value"
    assert decs[0].action == "redact"


def test_g15_counter_is_noop_without_otel():  # G15 — the increment never raises
    # In the default (no-OTel) env, _decisions_add takes the no-op path. A flagged decision
    # exercises it and must not raise (best-effort observability, never gates the decision).
    out = apply([_flag()], "input", msgs("hi"))
    assert len(out) == 1


def test_g15_counter_increments_with_otel(monkeypatch):  # G15 — real wire when OTel is present
    metrics = pytest.importorskip("opentelemetry.metrics")
    import cendor.guardrails as guardrails
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    monkeypatch.setattr(guardrails, "_decisions_counter", None)
    monkeypatch.setattr(guardrails, "_decisions_counter_checked", False)

    apply([_flag(name="pii")], "input", msgs("hi"))

    data = reader.get_metrics_data()
    points = [
        pt
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "cendor.guardrails.decisions"
        for pt in m.data.data_points
    ]
    assert points, "expected a cendor.guardrails.decisions counter data point"
    assert sum(pt.value for pt in points) >= 1
    attrs = dict(points[0].attributes)
    assert attrs.get("guardrail") == "pii"
    assert attrs.get("stage") == "input"
    assert attrs.get("action") == "flag"


def test_evaluate_runs_guardrails_in_order_and_carries_redaction():
    r1 = rules.regex_rule(r"aaa", action="redact", replacement="X", stage="input")
    r2 = rules.regex_rule(r"bbb", action="flag", stage="input")
    payload, decs = evaluate([r1, r2], "input", "aaa bbb")
    assert payload == "X bbb"  # r1's redaction is visible to r2
    assert [d.action for d in decs] == ["redact", "flag"]


def test_decision_shape_carries_context(decisions):
    ctx = Context(stage="input", agent="triage", tool="", trace_id="t-1", metadata={"k": "v"})
    apply([_flag(name="named")], "input", msgs("hi"), ctx)
    d = decisions[0]
    assert (d.guardrail, d.stage, d.action, d.agent, d.trace_id) == (
        "named",
        "input",
        "flag",
        "triage",
        "t-1",
    )
    assert d.metadata == {"k": "v"} and isinstance(d, GuardrailDecision)


def test_trace_id_falls_back_to_ambient(decisions):
    with trace("ambient-42"):
        apply([_flag()], "input", msgs("hi"))
    assert decisions[0].trace_id == "ambient-42"


def test_stage_filtering_skips_non_matching_guardrails(decisions):
    out = apply([_flag()], "output", msgs("hi"))  # _flag is input-only
    assert out == [] and decisions == []


def test_block_emits_decision_before_raising(decisions):
    with pytest.raises(GuardrailTripped):
        apply([_flag(name="warn"), _block()], "input", msgs("hi"))
    # both the pre-block flag and the block itself are on the bus, block last
    assert [d.action for d in decisions] == ["flag", "block"]


def test_sync_evaluate_rejects_async_check():
    async def acheck(payload, ctx):
        return None

    with pytest.raises(TypeError, match="async"):
        evaluate([rules.custom(acheck, name="a")], "input", "x")


async def test_evaluate_async_awaits_async_check(decisions):
    async def acheck(payload, ctx):
        return Verdict("flag", reason="async")

    out = await apply_async([rules.custom(acheck, name="a")], "input", "x")
    assert out[0].reason == "async" and decisions == out


async def test_evaluate_async_handles_sync_check_too():
    payload, decs = await evaluate_async([_flag()], "input", "x")
    assert decs[0].action == "flag"


async def test_evaluate_async_raises_on_block():
    with pytest.raises(GuardrailTripped):
        await apply_async([_block()], "input", "x")


def test_decorator_guardrail_runs_through_engine(decisions):
    @guardrail(stage="input")
    def deco_check(payload, ctx):
        return Verdict("flag", reason="deco")

    apply([deco_check], "input", "x")
    assert decisions[0].guardrail == "deco_check"
