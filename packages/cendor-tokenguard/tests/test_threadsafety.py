"""Thread-safety: concurrent record eviction accounting + cross-thread SQLite sink writes."""

import threading
from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument
from cendor.tokenguard import configure, dropped, report
from cendor.tokenguard.sinks import SQLiteSink


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


def test_concurrent_records_evict_consistently():
    # Every recorded call is accounted for exactly once under concurrent emits: retained + dropped
    # must equal the total, with no lost or double-counted rows from the non-atomic eviction.
    configure(max_records=50)
    client = _client()
    threads_n, per = 4, 100

    def worker():
        for _ in range(per):
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "x"}]
            )

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = threads_n * per
    retained = report(group_by=[]).rows[0]["calls"]
    assert retained == 50  # buffer stayed capped
    assert retained + dropped() == total  # nothing lost or double-counted


def test_sqlite_sink_cross_thread_write(tmp_path):
    # SQLiteSink is created on one thread but written from another (the bus is process-global).
    # Without check_same_thread=False + a lock this raises sqlite3.ProgrammingError.
    sink = SQLiteSink(str(tmp_path / "spend.db"))
    errors: list = []

    def worker(i):
        try:
            sink.write(
                {
                    "tags": {"w": i},
                    "usd": "0.01",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "model": "gpt-4o",
                }
            )
        except Exception as e:  # noqa: BLE001 - capture any cross-thread failure
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors  # no ProgrammingError from cross-thread use
    assert len(sink.rows()) == 8  # every concurrent write landed
    sink.close()
