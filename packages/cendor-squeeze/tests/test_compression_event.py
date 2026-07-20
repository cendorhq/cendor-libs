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
