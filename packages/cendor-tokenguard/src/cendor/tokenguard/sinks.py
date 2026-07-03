"""Pluggable spend sinks for tokenguard. docs/tokenguard.md §5.

A sink satisfies ``cendor.core.protocols.Sink`` (``write(entry)``). tokenguard's default is
in-memory (the ``report()`` aggregation); attach one of these to also persist each spend row.
Each ``write`` receives a dict:
``{"tags", "usd", "input_tokens", "output_tokens", "reasoning_tokens", "model"}`` — ``usd`` is the
Decimal as a string (never a float), and ``reasoning_tokens`` is a subset of ``output_tokens``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
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


class OTelSink:
    """Emit OpenTelemetry counters per spend row, if OpenTelemetry is installed (else a no-op)."""

    def __init__(self) -> None:
        self._tokens: Any = None
        self._cost: Any = None
        self._reasoning: Any = None
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
        attrs = {"model": entry.get("model", "")}
        # reasoning is a subset of output — reported as its own counter, not added into the total.
        self._tokens.add(int(entry["input_tokens"]) + int(entry["output_tokens"]), attrs)
        self._reasoning.add(int(entry.get("reasoning_tokens", 0)), attrs)
        self._cost.add(float(entry["usd"]), attrs)
