import pytest
from cendor.core import bus


def test_emit_reaches_subscriber():
    bus._reset()
    seen = []
    bus.subscribe(seen.append)
    bus.emit("hello")
    assert seen == ["hello"]


def test_unsubscribe_stops_delivery():
    bus._reset()
    seen = []
    fn = seen.append
    bus.subscribe(fn)
    bus.unsubscribe(fn)
    bus.emit("after")
    assert seen == []
    bus.unsubscribe(fn)  # idempotent — no error when already absent


def test_has_subscribers_tracks_registration():
    """Public accessor (not the `_subscriber_count` test helper): lets an emitter skip building an
    expensive event nobody would receive — squeeze gates its CompressionEvent token counts on it."""
    bus._reset()
    assert bus.has_subscribers() is False
    fn = bus.subscribe(lambda e: None)
    assert bus.has_subscribers() is True
    bus.unsubscribe(fn)
    assert bus.has_subscribers() is False


def test_emit_runs_every_subscriber_even_if_one_raises():
    # One tool's failure must not starve another (e.g. a logging bug skipping enforcement); the
    # first exception still propagates so intentional control flow (tokenguard) reaches the caller.
    bus._reset()
    ran = []
    bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("subscriber bug")))
    bus.subscribe(ran.append)
    with pytest.raises(RuntimeError):
        bus.emit("x")
    assert ran == ["x"]  # the second subscriber still ran
