"""Attribution: track(...) tags ambient spend; report(group_by) aggregates it. No network."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument
from cendor.tokenguard import report, track


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    yield
    bus._reset()
    tokenguard.reset()


def _client():
    """gpt-4o: $0.0075 per call (1000 in / 500 out)."""

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def _call(client):
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])


def test_report_groups_by_tags():
    client = _client()
    with track(feature="support", user_id="alice"):
        _call(client)
    with track(feature="support", user_id="bob"):
        _call(client)
    with track(feature="billing", user_id="alice"):
        _call(client)

    by_feature = {tuple(r["tags"].items()): r for r in report(group_by=["feature"])}
    assert by_feature[(("feature", "support"),)]["calls"] == 2
    assert by_feature[(("feature", "support"),)]["usd"].amount == Decimal("0.015")
    assert by_feature[(("feature", "billing"),)]["calls"] == 1

    rows = list(report(group_by=["feature", "user_id"]))
    assert len(rows) == 3  # support/alice, support/bob, billing/alice


def test_report_total_and_tokens():
    client = _client()
    with track(feature="x"):
        _call(client)
        _call(client)
    r = report(group_by=["feature"])
    assert r.total().amount == Decimal("0.015")
    assert r.rows[0]["tokens"] == 3000  # (1000 + 500) * 2


def test_nested_track_merges_tags():
    client = _client()
    with track(feature="support"):
        with track(user_id="alice"):
            _call(client)
    row = report(group_by=["feature", "user_id"]).rows[0]
    assert row["tags"] == {"feature": "support", "user_id": "alice"}


def test_assert_under_passes_and_fails():
    client = _client()
    with track(feature="support"):
        _call(client)  # $0.0075
    r = report(group_by=["feature"])
    assert r.assert_under(usd=0.01, feature="support") is True
    with pytest.raises(AssertionError):
        r.assert_under(usd=0.001, feature="support")


def test_track_report_alias():
    # track.report is the documented ergonomic alias for report().
    assert track.report is report


def test_sqlite_sink_persists_each_row(tmp_path):
    from cendor.core import protocols
    from cendor.tokenguard.sinks import SQLiteSink

    sink = SQLiteSink(str(tmp_path / "spend.db"))
    assert isinstance(sink, protocols.Sink)  # satisfies the core Sink protocol by shape
    tokenguard.use_sink(sink)
    try:
        client = _client()
        with track(feature="support", user_id="alice"):
            _call(client)
            _call(client)
    finally:
        tokenguard.use_sink(None)

    rows = sink.rows()
    assert len(rows) == 2  # one persisted row per call
    tags_json, usd, inp, out, reasoning, model = rows[0]
    assert model == "gpt-4o"
    assert Decimal(usd) == Decimal("0.0075")  # Decimal stored as a string, never a float
    assert reasoning == 0  # gpt-4o here reports no reasoning tokens
    assert '"feature":"support"' in tags_json.replace(" ", "")
    sink.close()


def test_otel_sink_is_noop_without_otel():
    from cendor.tokenguard.sinks import OTelSink

    sink = OTelSink()  # OTel not installed in tests
    sink.write(
        {"tags": {}, "usd": "0.01", "input_tokens": 1, "output_tokens": 1, "model": "gpt-4o"}
    )


class _FakeCounter:
    """Captures (amount, attributes) so the sink's dimensioning is testable without an OTel SDK."""

    def __init__(self, sink_calls):
        self._calls = sink_calls

    def add(self, amount, attrs):
        self._calls.append((amount, dict(attrs)))


def test_otel_sink_dimensions_include_track_tags():
    # G9: spend counters must be dimensioned by attribution tags, not model alone.
    from cendor.tokenguard.sinks import OTelSink

    sink = OTelSink()
    calls: list = []
    sink._tokens = sink._reasoning = sink._cost = _FakeCounter(calls)
    sink.write(
        {
            "tags": {"feature": "support", "user_id": "alice"},
            "usd": "0.01",
            "input_tokens": 10,
            "output_tokens": 5,
            "reasoning_tokens": 0,
            "model": "gpt-4o",
        }
    )
    _amount, attrs = calls[0]
    assert attrs["model"] == "gpt-4o"
    assert attrs["feature"] == "support"
    assert attrs["user_id"] == "alice"


def test_otel_sink_tags_false_emits_only_model():
    from cendor.tokenguard.sinks import OTelSink

    sink = OTelSink(tags=False)
    calls: list = []
    sink._tokens = sink._reasoning = sink._cost = _FakeCounter(calls)
    sink.write(
        {
            "tags": {"feature": "support"},
            "usd": "0.01",
            "input_tokens": 1,
            "output_tokens": 1,
            "model": "m",
        }
    )
    _amount, attrs = calls[0]
    assert attrs == {"model": "m"}  # tags suppressed to keep metric cardinality bounded
