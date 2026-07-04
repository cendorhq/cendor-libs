"""WS-C: QueueSink moves durable sink I/O off the hot path; Sink.flush/close are optional.

The bus runs subscribers inline, so a durable sink adds its I/O latency to every model call.
``QueueSink`` wraps any sink and drains it on a background thread — ``write()`` returns immediately,
ordering is preserved, and ``flush()``/``close()`` guarantee durability at shutdown. No network.
"""

import time
from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus
from cendor.core.protocols import Sink
from cendor.core.types import LLMCall, Usage
from cendor.tokenguard.sinks import QueueSink, SQLiteSink


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    yield
    bus._reset()
    tokenguard.reset()


class _ListSink:
    """A minimal write-only sink (records order and optionally the calling thread)."""

    def __init__(self):
        self.rows = []

    def write(self, entry):
        self.rows.append(entry)


class _LifecycleSink(_ListSink):
    def __init__(self):
        super().__init__()
        self.flushed = 0
        self.closed = 0

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed += 1


def test_queue_sink_drains_on_close_in_order():
    inner = _ListSink()
    q = QueueSink(inner)
    for i in range(200):
        q.write(i)
    q.close()
    assert inner.rows == list(range(200))  # every row, in FIFO order


def test_queue_sink_flush_drains_without_closing():
    inner = _ListSink()
    q = QueueSink(inner)
    for i in range(50):
        q.write(i)
    q.flush()
    assert inner.rows == list(range(50))
    q.write(50)  # still usable after flush
    q.flush()
    assert inner.rows == list(range(51))
    q.close()


def test_slow_inner_sink_does_not_block_write():
    class _SlowSink:
        def __init__(self):
            self.rows = []

        def write(self, entry):
            time.sleep(0.05)  # 50ms of "durable I/O" per row
            self.rows.append(entry)

    inner = _SlowSink()
    q = QueueSink(inner)

    t0 = time.perf_counter()
    for i in range(20):
        q.write(i)
    enqueue_elapsed = time.perf_counter() - t0

    # Enqueuing 20 rows returns fast — far less than the ~1s the inner sink needs to write them.
    assert enqueue_elapsed < 0.5
    assert len(inner.rows) < 20  # proof the writes are asynchronous, not inline

    q.flush()  # now block until the worker has drained everything
    assert inner.rows == list(range(20))
    q.close()


def test_close_flushes_then_closes_inner():
    inner = _LifecycleSink()
    q = QueueSink(inner)
    q.write("a")
    q.close()
    assert inner.rows == ["a"]
    assert inner.flushed == 1 and inner.closed == 1  # inner lifecycle propagated
    q.close()  # idempotent — no second flush/close
    assert inner.flushed == 1 and inner.closed == 1


def test_write_after_close_raises():
    q = QueueSink(_ListSink())
    q.close()
    with pytest.raises(RuntimeError):
        q.write("x")


def test_context_manager_drains_on_exit():
    inner = _ListSink()
    with QueueSink(inner) as q:
        for i in range(10):
            q.write(i)
    assert inner.rows == list(range(10))  # __exit__ called close() → drained


def test_optional_sink_members_are_detected_via_hasattr():
    write_only = SimpleNamespace(write=lambda e: None)
    assert isinstance(write_only, Sink)  # write-only still satisfies the protocol
    assert not hasattr(write_only, "flush")

    q = QueueSink(_ListSink())
    assert isinstance(q, Sink)  # QueueSink is a Sink too
    assert callable(getattr(q, "flush", None)) and callable(getattr(q, "close", None))
    q.close()


def test_max_queue_applies_backpressure_without_dropping():
    inner = _ListSink()
    q = QueueSink(inner, max_queue=4)
    for i in range(50):
        q.write(i)  # blocks when the bounded queue is full — never drops a row
    q.close()
    assert inner.rows == list(range(50))  # all 50 preserved despite the small queue


def test_queue_sink_wraps_sqlite_through_the_bus(tmp_path):
    # End-to-end: durable spend logging via tokenguard's sink seam, off the hot path.
    inner = SQLiteSink(str(tmp_path / "spend.db"))
    q = QueueSink(inner)
    tokenguard.use_sink(q)
    for i in range(5):
        bus.emit(
            LLMCall(
                id=str(i),
                provider="openai",
                model="gpt-4o",
                messages=[],
                usage=Usage(input_tokens=10, output_tokens=5),
            )
        )
    q.flush()  # drain the queue before reading back
    assert len(inner.rows()) == 5
    tokenguard.use_sink(None)
    q.close()
