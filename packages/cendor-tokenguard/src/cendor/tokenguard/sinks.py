"""Pluggable spend sinks for tokenguard. docs/tokenguard.md §5.

A sink satisfies ``cendor.core.protocols.Sink`` (``write(entry)``). tokenguard's default is
in-memory (the ``report()`` aggregation); attach one of these to also persist each spend row.
Each ``write`` receives a dict:
``{"tags", "usd", "input_tokens", "output_tokens", "reasoning_tokens", "model"}`` — ``usd`` is the
Decimal as a string (never a float), and ``reasoning_tokens`` is a subset of ``output_tokens``.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Callable
from typing import Any


class SQLiteSink:
    """Persist each spend row to a local SQLite database (stdlib; no network, no heavy dep).

    Instrumented calls can emit from any thread (the bus is process-global), so the connection is
    opened with ``check_same_thread=False`` and every write/read is serialized under a lock — a
    cross-thread ``emit`` would otherwise raise ``sqlite3.ProgrammingError``.
    """

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS spend ("
                "tags TEXT, usd TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                "reasoning_tokens INTEGER, model TEXT)"
            )
            self.conn.commit()

    def write(self, entry: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO spend "
                "(tags, usd, input_tokens, output_tokens, reasoning_tokens, model) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    json.dumps(entry.get("tags", {}), sort_keys=True),
                    entry["usd"],
                    entry["input_tokens"],
                    entry["output_tokens"],
                    entry.get("reasoning_tokens", 0),
                    entry["model"],
                ),
            )
            self.conn.commit()

    def rows(self) -> list[tuple]:
        """All rows: ``(tags_json, usd, input_tokens, output_tokens, reasoning_tokens, model)``."""
        with self._lock:
            return self.conn.execute(
                "SELECT tags, usd, input_tokens, output_tokens, reasoning_tokens, model "
                "FROM spend ORDER BY rowid"
            ).fetchall()

    def close(self) -> None:
        with self._lock:
            self.conn.close()


#: Sentinel enqueued by :meth:`QueueSink.close` to tell the drain worker to stop.
_SHUTDOWN: Any = object()


class QueueSink:
    """Wrap any :class:`~cendor.core.protocols.Sink` so its writes run on a background thread.

    The bus fans out to subscribers **inline**, so a durable sink (SQLite/OTel/file) otherwise adds
    its I/O latency to every model call. ``QueueSink`` decouples that: ``write()`` enqueues and
    returns immediately, while a single daemon worker drains the queue into the inner sink **in
    order** (FIFO — ordering is preserved). Wrap any existing sink:
    ``use_sink(QueueSink(SQLiteSink(path)))``.

    Durability is opt-in at shutdown: ``flush()`` blocks until the queue is empty and the inner sink
    is flushed; ``close()`` flushes, stops the worker, and closes the inner sink. Call one of them
    (or use the sink as a context manager) before exit so no tail records are lost — the worker is a
    *daemon*, so an abrupt process exit without ``close()`` can drop still-queued rows. Both are the
    optional :class:`~cendor.core.protocols.Sink` lifecycle methods.

    ``max_queue`` bounds the queue; when it's full, ``write()`` **blocks** until the worker drains
    room (back-pressure — a spend/audit row is never silently dropped). ``None`` (default) is
    unbounded.

    A row *can* still be lost the other way: if the inner sink's ``write`` **raises** (disk full, DB
    locked), the offending row is dropped so the failure doesn't kill the worker. Those drops are
    observable — :attr:`dropped_rows` counts them, and an optional ``on_drop_error(exc, entry)``
    callback fires for each (its own exceptions are swallowed too, so a broken callback can't kill
    the worker either).
    """

    def __init__(
        self,
        inner: Any,
        *,
        max_queue: int | None = None,
        on_drop_error: Callable[[Exception, Any], None] | None = None,
    ) -> None:
        self._inner = inner
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue or 0)
        self._closed = False
        self._lock = threading.Lock()
        self._on_drop_error = on_drop_error
        self._dropped = 0  # written only by the single drain worker; read lock-free elsewhere
        self._worker = threading.Thread(target=self._drain, name="cendor-queuesink", daemon=True)
        self._worker.start()

    @property
    def dropped_rows(self) -> int:
        """Number of rows discarded because the inner sink's ``write`` raised (never kills the
        worker). ``0`` in the healthy path; a rising count flags a failing durable sink."""
        return self._dropped

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SHUTDOWN:
                    return
                self._inner.write(item)
            except Exception as exc:  # noqa: BLE001 - a bad row must not kill the worker (count it)
                self._dropped += 1
                if self._on_drop_error is not None:
                    try:
                        self._on_drop_error(exc, item)
                    except Exception:  # noqa: BLE001 - a broken callback must not kill the worker
                        pass
            finally:
                self._queue.task_done()

    def write(self, entry: Any) -> None:
        """Enqueue a record for the worker (returns at once; blocks only when the queue is full)."""
        if self._closed:
            raise RuntimeError("QueueSink.write() after close()")
        self._queue.put(entry)

    def flush(self) -> None:
        """Block until every queued record is written and the inner sink is flushed."""
        self._queue.join()
        inner_flush = getattr(self._inner, "flush", None)
        if callable(inner_flush):
            inner_flush()

    def close(self) -> None:
        """Drain the queue, stop the worker, and flush + close the inner sink (idempotent)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(
            _SHUTDOWN
        )  # after all enqueued rows (FIFO) — worker drains them, then exits
        self._worker.join()
        for name in ("flush", "close"):  # flush then release the inner sink's resources
            fn = getattr(self._inner, name, None)
            if callable(fn):
                fn()

    def __enter__(self) -> QueueSink:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class OTelSink:
    """Emit OpenTelemetry counters per spend row, if OpenTelemetry is installed (else a no-op).

    Each counter is dimensioned by ``model`` **and** by the active ``track(...)`` tags (feature /
    user_id / …), so a metrics backend can break spend down by attribution — the same slice
    ``report(group_by=[…])`` gives you locally. Tag *values* become metric attributes, so keep them
    **low-cardinality** (``feature``, ``env``, ``tenant`` — not a raw per-user id) or your backend's
    time-series count can explode; pass ``tags=False`` to emit ``model`` only. Metric names port the
    Python originals byte-for-byte to the TypeScript ``OTelSink``.
    """

    #: Marks this class as the OTel spend emitter, so tokenguard's internal telemetry tap can stand
    #: down when the user has already wired one themselves (no double-counted spend).
    _cendor_otel_spend = True

    def __init__(self, *, tags: bool = True, meter: Any = None) -> None:
        """
        Args:
            tags: Dimension the counters by the active ``track(...)`` tags as well as ``model``
                (default). ``False`` emits ``model`` only — use it when tag values are
                high-cardinality.
            meter: An explicit OpenTelemetry ``Meter`` to create the counters on. Omit it — the
                default — and they come from the **global** provider via
                ``metrics.get_meter("cendor.tokenguard")``, exactly as before. Pass one to send
                metrics somewhere the global provider isn't: a test's in-memory reader, an isolated
                provider in a multi-tenant host, or a second pipeline. Counter names, attributes and
                the no-OTel no-op are identical either way.

                Injection exists because there was no way to read these counters without installing
                a process-global meter provider — filed as a product improvement by the external
                suite, which had to install one to assert anything.
        """
        self._tokens: Any = None
        self._cost: Any = None
        self._reasoning: Any = None
        self._tags = tags
        if meter is None:
            try:
                from opentelemetry import metrics
            except ImportError:
                return
            meter = metrics.get_meter("cendor.tokenguard")
        self._tokens = meter.create_counter("gen_ai.client.token.usage")
        self._cost = meter.create_counter("gen_ai.client.cost.usd")
        self._reasoning = meter.create_counter("gen_ai.client.reasoning.token.usage")

    def write(self, entry: dict) -> None:
        if self._tokens is None:
            return  # OTel not installed — silently skip
        attrs: dict[str, Any] = {"model": entry.get("model", "")}
        if self._tags:
            # Attribution dimensions: flatten low-cardinality tag values (str/num/bool) so spend is
            # sliceable by feature/tenant in the backend. Non-primitive values are stringified.
            for key, value in (entry.get("tags") or {}).items():
                prim = value if isinstance(value, (bool, int, float, str)) else str(value)
                attrs[str(key)] = prim
        # reasoning is a subset of output — reported as its own counter, not added into the total.
        self._tokens.add(int(entry["input_tokens"]) + int(entry["output_tokens"]), attrs)
        self._reasoning.add(int(entry.get("reasoning_tokens", 0)), attrs)
        self._cost.add(float(entry["usd"]), attrs)
