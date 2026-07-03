"""The bus survives concurrent (un)subscribe + emit from many threads without losing events."""

import threading

from cendor.core import bus


def setup_function():
    bus._reset()


def teardown_function():
    bus._reset()


def test_concurrent_emit_and_subscribe_no_corruption():
    counts: dict[int, int] = {}
    lock = threading.Lock()

    def make_sub(i):
        def sub(_event):
            with lock:
                counts[i] = counts.get(i, 0) + 1

        return sub

    subs = [make_sub(i) for i in range(20)]
    barrier = threading.Barrier(2)

    def churn():
        barrier.wait()
        for _ in range(200):
            for s in subs:
                bus.subscribe(s)  # idempotent
                bus.unsubscribe(s)

    def emitter():
        barrier.wait()
        for _ in range(200):
            bus.emit({"x": 1})

    # A stable subscriber that must receive every emit regardless of churn on the others.
    received = []
    bus.subscribe(received.append)

    t1 = threading.Thread(target=churn)
    t2 = threading.Thread(target=emitter)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(received) == 200  # no emit dropped or duplicated; list never corrupted


def test_subscriber_may_unsubscribe_itself_during_emit():
    # A callback that unsubscribes itself mid-emit must not deadlock (emit holds no lock while
    # fanning out — it snapshots under the lock, then releases before invoking subscribers).
    hits = []

    def once(_event):
        hits.append(1)
        bus.unsubscribe(once)

    bus.subscribe(once)
    bus.emit("a")
    bus.emit("b")
    assert hits == [1]  # ran once, then cleanly removed itself
