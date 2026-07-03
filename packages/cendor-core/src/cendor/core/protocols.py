"""Structural interfaces shared across the stack. docs/core.md §2, architecture.md §2 (Layer 1).

These are ``typing.Protocol``s, so a library satisfies an interface by *shape* — no imports, no
base classes, zero directional coupling. ``squeeze`` *is* a ``Compressor`` without importing
``contextkit``; ``acttrace`` *is* a ``Subscriber`` without importing the bus' producers.

Grown incrementally as tools land: ``Compressor``/``EvictionStrategy``/``Handle`` (contextkit +
squeeze), ``Sink``/``Subscriber`` (cassette + acttrace).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Handle(Protocol):
    """A restore handle for a reversible compression. ``expand()`` returns the original."""

    def expand(self) -> Any: ...


@runtime_checkable
class Compressor(Protocol):
    """Shrinks content toward a token budget and returns a restorable :class:`Handle`.

    ``squeeze`` satisfies this; ``contextkit`` accepts anything of this shape for
    ``Block(evict="compress")`` without importing ``squeeze``.
    """

    def compress(
        self,
        content: Any,
        *,
        target_tokens: int | None = None,
        model: str | None = None,
        kind: str = "auto",
    ) -> tuple[str, Handle]: ...


@runtime_checkable
class EvictionStrategy(Protocol):
    """A pluggable per-block shrink rule. Returns ``(new_content_or_None, action_label)``.

    ``None`` content means the block was dropped. ``contextkit`` ships string-named built-ins
    (``drop_oldest``/``truncate``) and accepts custom strategies of this shape.
    """

    def evict(self, content: str, remaining_tokens: int, model: str) -> tuple[str | None, str]: ...


@runtime_checkable
class Sink(Protocol):
    """A destination for records/entries (in-memory, JSONL, SQLite, OTel, ...)."""

    def write(self, entry: Any) -> None: ...


@runtime_checkable
class Subscriber(Protocol):
    """A bus subscriber: a callable that receives normalized events. ``acttrace`` is one."""

    def __call__(self, event: Any) -> None: ...
