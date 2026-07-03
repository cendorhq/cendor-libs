"""In-process pub/sub event bus: one instrument() emits, many tools subscribe. docs/core.md §6.

Thread-safe within a process: the subscriber list is guarded by a lock for registration changes,
and :func:`emit` fans out over a snapshot taken under that lock — so subscribers may (un)subscribe
from other threads (or from inside a callback) without corrupting the list or deadlocking.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

_subscribers: list[Callable[[Any], None]] = []
_lock = threading.Lock()


def subscribe(fn: Callable[[Any], None]) -> Callable[[Any], None]:
    """Register a subscriber. Usable as a decorator. Idempotent: re-registering the
    same callable is a no-op, so a sibling tool can safely ensure its subscription."""
    with _lock:
        if fn not in _subscribers:
            _subscribers.append(fn)
    return fn


def unsubscribe(fn: Callable[[Any], None]) -> None:
    """Remove a subscriber (no error if absent) — the inverse of :func:`subscribe`.

    Lets a tool register a *temporary* subscriber (e.g. cassette's recorder) and tear it down
    cleanly, without reaching into the internal subscriber list."""
    with _lock:
        if fn in _subscribers:
            _subscribers.remove(fn)


def emit(event: Any) -> None:
    """Publish an event to every subscriber (synchronous).

    Every subscriber runs even if an earlier one raises, so one tool's failure can't starve
    another (a logging subscriber's bug must not skip ``tokenguard``'s enforcement, or vice versa).
    The first ``Exception`` raised is re-raised after all subscribers have run, so intentional
    control flow (e.g. ``tokenguard``'s post-flight ``BudgetExceeded``) still reaches the caller.
    ``BaseException`` (``KeyboardInterrupt``/``SystemExit``) is not caught — it propagates at once.

    The fan-out runs over a snapshot taken under the lock, then *releases* it before invoking any
    subscriber — so a subscriber is free to (un)subscribe without deadlocking, and a slow one never
    blocks registration on another thread.
    """
    with _lock:
        subscribers = list(_subscribers)
    first_exc: Exception | None = None
    for fn in subscribers:
        try:
            fn(event)
        except Exception as exc:  # noqa: BLE001 - isolate subscribers, re-raise first after all run
            if first_exc is None:
                first_exc = exc
    if first_exc is not None:
        raise first_exc


def _reset() -> None:
    """Test helper: clear all subscribers."""
    with _lock:
        _subscribers.clear()
