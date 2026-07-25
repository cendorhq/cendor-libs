"""The internal OTel spend tap (DR-3): spend reaches your backend with zero telemetry code.

The tap sits **beside** the user's `use_sink` slot, never in it — `use_sink` replaces, so wiring the
automatic export through that slot would mean a user's later `use_sink(SQLiteSink(...))` silently
switched backend spend off. Under `CENDOR_TELEMETRY=off` the tap never runs; without OpenTelemetry
installed it is inert.

No network: a fake client + an in-memory metric reader (fixtures in the workspace conftest).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.otel import TELEMETRY_ENV
from cendor.tokenguard import reset, track, use_sink


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(TELEMETRY_ENV, raising=False)
    bus._reset()
    reset()
    yield
    reset()
    bus._reset()


def _client(prompt=1000, completion=500):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _points(reader, name: str) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        pt
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == name
        for pt in m.data.data_points
    ]


def _call(client, model="gpt-4o"):
    client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}])


def test_zero_code_app_records_spend_counters(otel_metrics):
    client = instrument(_client())
    _call(client)
    tokens = _points(otel_metrics, "gen_ai.client.token.usage")
    cost = _points(otel_metrics, "gen_ai.client.cost.usd")
    assert sum(pt.value for pt in tokens) == 1500
    assert sum(pt.value for pt in cost) > 0
    assert dict(tokens[0].attributes)["model"] == "gpt-4o"


def test_the_tap_carries_track_tags_for_attribution(otel_metrics):
    client = instrument(_client())
    with track(feature="refunds", user_id="alice"):
        _call(client)
    attrs = dict(_points(otel_metrics, "gen_ai.client.cost.usd")[0].attributes)
    assert attrs["feature"] == "refunds"
    assert attrs["user_id"] == "alice"


def test_the_user_sink_and_the_tap_both_receive_every_row(otel_metrics):
    rows: list[dict] = []
    use_sink(SimpleNamespace(write=rows.append))
    client = instrument(_client())
    _call(client)
    assert len(rows) == 1, "the user's sink still sees the row"
    assert sum(pt.value for pt in _points(otel_metrics, "gen_ai.client.token.usage")) == 1500


def test_clearing_the_user_sink_does_not_kill_the_tap(otel_metrics):
    client = instrument(_client())
    use_sink(SimpleNamespace(write=lambda row: None))
    use_sink(None)  # the documented way to detach YOUR sink
    _call(client)
    assert _points(otel_metrics, "gen_ai.client.token.usage"), "backend spend is unaffected"


def test_off_kills_the_tap(otel_metrics, monkeypatch):
    monkeypatch.setenv(TELEMETRY_ENV, "off")
    client = instrument(_client())
    _call(client)
    assert _points(otel_metrics, "gen_ai.client.token.usage") == []


def test_without_otel_the_tap_is_inert(no_otel):
    client = instrument(_client())
    _call(client)  # must not raise — the tap builds an OTelSink that is a no-op without OTel


def test_an_explicit_otel_sink_makes_the_tap_stand_down(otel_metrics):
    """Today's docs say `use_sink(sinks.OTelSink())`, so an app that upgrades keeps that line. The
    tap
    must then stand down — otherwise every counter would double the moment they upgraded."""
    from cendor.tokenguard.sinks import OTelSink

    use_sink(OTelSink())
    client = instrument(_client())
    _call(client)
    total = sum(pt.value for pt in _points(otel_metrics, "gen_ai.client.token.usage"))
    assert total == 1500, "counted once — the user's sink, not the user's sink plus the tap"


def test_a_queued_otel_sink_also_makes_the_tap_stand_down(otel_metrics):
    from cendor.tokenguard.sinks import OTelSink, QueueSink

    q = QueueSink(OTelSink())
    use_sink(q)
    client = instrument(_client())
    _call(client)
    q.close()  # drain
    total = sum(pt.value for pt in _points(otel_metrics, "gen_ai.client.token.usage"))
    assert total == 1500, "the wrapper is unwrapped, so a QueueSink(OTelSink()) is recognised too"
