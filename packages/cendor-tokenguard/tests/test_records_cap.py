"""The in-memory spend buffer is bounded (FIFO) so a long-running process can't grow it forever."""

from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument
from cendor.tokenguard import configure, dropped, report


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    yield
    bus._reset()
    tokenguard.reset()


def _client():
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_cap_evicts_oldest_and_counts_drops():
    configure(max_records=5)
    client = _client()
    for _ in range(12):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])

    rows = report()
    assert rows.total().amount > 0  # still aggregates the retained window
    assert dropped() == 7  # 12 calls, cap 5 -> 7 oldest evicted, counted (never silent)
    # the buffer is bounded: report reflects only the retained 5 rows
    assert report(group_by=[]).rows[0]["calls"] == 5


def test_unbounded_when_cap_disabled():
    configure(max_records=None)
    client = _client()
    for _ in range(20):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])
    assert dropped() == 0
    assert report(group_by=[]).rows[0]["calls"] == 20


def test_reset_restores_default_cap_and_clears_drops():
    configure(max_records=2)
    client = _client()
    for _ in range(5):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])
    assert dropped() == 3

    tokenguard.reset()
    assert dropped() == 0  # cleared
    # default cap is high enough that ordinary use never drops
    for _ in range(5):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])
    assert dropped() == 0
