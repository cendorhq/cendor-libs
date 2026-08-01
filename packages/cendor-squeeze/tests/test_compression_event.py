"""compress() emits a metadata-only CompressionEvent on the bus (G21). Never any text."""

import pytest
from cendor.core import bus
from cendor.squeeze import CompressionEvent, compress


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


def test_compress_emits_metadata_only_event():
    seen: list = []
    bus.subscribe(seen.append)
    data = {"user": {"id": 42, "name": "Ada"}, "scores": [1, 2, 3, 4, 5], "nulls": None}
    small, handle = compress(data, kind="json")

    events = [e for e in seen if isinstance(e, CompressionEvent)]
    assert len(events) == 1
    ev = events[0]
    assert ev.handle_id == handle.id
    assert ev.kind == "json"
    assert ev.technique  # non-empty
    assert ev.tokens_before >= ev.tokens_after >= 0
    assert 0.0 <= ev.ratio <= 1.0
    assert ev.store_kind == "MemoryStore"
    # The event carries NO content — only counts/ids/technique. The original text never rides it.
    for value in vars(ev).values():
        if isinstance(value, str):
            assert "Ada" not in value


def test_no_subscribers_means_no_token_counting(monkeypatch):
    """With NOTHING subscribed, compress() must not count tokens for the event at all.

    The counting is ~93% of a large compress() — measured, see the closed card
    BUG-squeeze-compression-event-token-cost — and an event with no subscriber is unobservable by
    definition. Spy on tokens.count and demand zero calls from the emit path."""
    import cendor.squeeze as squeeze_mod

    calls: list = []
    real_count = squeeze_mod.tokens.count

    def counting_spy(text, model=None, *a, **kw):
        calls.append(text)
        return real_count(text, model, *a, **kw)

    monkeypatch.setattr(squeeze_mod.tokens, "count", counting_spy)
    data = {"user": {"id": 42, "name": "Ada"}, "scores": list(range(50))}
    small, handle = compress(data, kind="json")
    assert small and handle.expand()  # compression itself is unaffected
    assert calls == [], f"tokens.count ran {len(calls)}× with zero subscribers"


def test_one_subscriber_gets_correct_counts(monkeypatch):
    """One subscriber: the event still carries correct counts — exactly two tokens.count calls."""
    import cendor.squeeze as squeeze_mod

    calls: list = []
    real_count = squeeze_mod.tokens.count

    def counting_spy(text, model=None, *a, **kw):
        calls.append(text)
        return real_count(text, model, *a, **kw)

    monkeypatch.setattr(squeeze_mod.tokens, "count", counting_spy)
    seen: list = []
    bus.subscribe(seen.append)
    data = {"user": {"id": 42, "name": "Ada"}, "scores": list(range(50))}
    small, _handle = compress(data, kind="json")

    events = [e for e in seen if isinstance(e, CompressionEvent)]
    assert len(events) == 1
    ev = events[0]
    assert len(calls) == 2  # original + compressed, nothing more
    assert ev.tokens_before == real_count(calls[0], "gpt-4o")
    assert ev.tokens_after == real_count(small, "gpt-4o")
    assert ev.ratio == (ev.tokens_after / ev.tokens_before if ev.tokens_before else 1.0)
