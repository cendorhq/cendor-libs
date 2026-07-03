"""Content-addressed store (CCR) backends for squeeze. docs/squeeze.md §5, §7.

The original of every compression is kept keyed by its hash so ``handle.expand()`` is exact.
``MemoryStore`` (the default) keeps originals in-process; ``SQLiteStore`` persists them to a local
file so they survive the process and dedupe across runs — both stdlib, no network. A backend is
any object with ``get(key) -> str`` and ``put(key, value) -> None``; swap one in via
``squeeze.use_store(...)``.
"""

from __future__ import annotations

import sqlite3


class MemoryStore:
    """In-process CCR store (the default). Fast, ephemeral, deduped by key.

    ``max_items`` bounds the store with a **least-recently-used (LRU)** policy: reading a key
    (``get``) or re-storing it (``put``) refreshes its recency, so a handle you keep expanding
    survives eviction and only genuinely-cold originals are dropped. Expanding a handle whose
    original was evicted raises ``KeyError`` — the documented trade-off of a capped store. ``None``
    (default) means unbounded (no eviction, so recency isn't tracked).
    """

    def __init__(self, max_items: int | None = None) -> None:
        self._data: dict[str, str] = {}
        self._max = max_items

    def get(self, key: str) -> str:
        value = self._data[key]
        if self._max is not None:  # LRU: mark as most-recently-used
            self._data[key] = self._data.pop(key)
        return value

    def put(self, key: str, value: str) -> None:
        if key in self._data:
            if self._max is not None:  # re-put refreshes recency
                self._data[key] = self._data.pop(key)
            return
        self._data[key] = value
        if self._max is not None:
            while len(self._data) > self._max:
                del self._data[next(iter(self._data))]  # evict the least-recently-used

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)


class SQLiteStore:
    """Local SQLite CCR store: originals persist across processes, deduped by key (stdlib).

    Opened with ``check_same_thread=False`` so a single store can serve a threaded server; writes
    are idempotent ``INSERT OR IGNORE``s (content-addressed), so concurrent puts of the same content
    are safe.
    """

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS ccr (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.commit()

    def get(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM ccr WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0]

    def put(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO ccr (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def __contains__(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM ccr WHERE key = ?", (key,)).fetchone() is not None

    def __len__(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM ccr").fetchone()[0])

    def close(self) -> None:
        self.conn.close()
