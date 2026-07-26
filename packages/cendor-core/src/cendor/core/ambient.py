"""Ambient metadata providers — the one core-owned seam for stamping run-scoped context onto every
event at its single guaranteed-correct capture moment: **event construction, in the caller's
synchronous frame, before interceptors run.** The ``trace_id`` has always been captured there; this
generalizes it to everything else (agent, conversation id, decision id, attribution tags, budget
frames, cassette session) that would otherwise be re-read at bus-delivery time — a read that breaks
under streams finalized outside the originating scope, context-losing layers, subscriber order, and
concurrent runs (and, in Python, generators that leak run scopes into the consumer).

A provider is a ``(event) -> dict | None`` callable. :func:`apply_ambient` runs the registered
providers over a freshly built event and merges their metadata onto ``event.metadata``, in
registration order, **never overwriting an existing key**. The event is passed read-only so a
provider may key a ``WeakKeyDictionary`` off it for non-serializable attachments (frames, handles)
instead of returning them. Contract: **never-raise** (a provider's exception is swallowed) and a
**zero-provider fast path of a single length check** (the standalone-libs byte-identity + the
benchmark row both hold when nothing is registered).

Core stays generic: it merges opaque metadata and learns no SDK vocabulary.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

AmbientProvider = Callable[[Any], "dict[str, Any] | None"]

_providers: list[AmbientProvider] = []
_lock = threading.Lock()


def add_ambient_provider(fn: AmbientProvider) -> AmbientProvider:
    """Register an ambient metadata provider (idempotent). The provider runs synchronously at every
    event's construction site — the frame where run context (contextvars / trace scope) is
    unconditionally correct — so values it stamps survive delivery no matter when or where the event
    is finalized.

    ```python
    from cendor.core import add_ambient_provider
    add_ambient_provider(lambda event: {"agent": "reviewer", "tenant": "acme"})
    ```
    """
    with _lock:
        if fn not in _providers:
            _providers.append(fn)
    return fn


def remove_ambient_provider(fn: AmbientProvider) -> None:
    """Unregister a previously added ambient provider (no error if absent)."""
    with _lock:
        if fn in _providers:
            _providers.remove(fn)


def apply_ambient(event: Any) -> None:
    """Merge every registered provider's metadata onto ``event.metadata``, in registration order,
    never overwriting a key already present. Internal — invoked by core at every event-construction
    site. Zero-provider fast path is a single length check."""
    if not _providers:
        return
    with _lock:
        providers = list(_providers)
    meta = event.metadata
    for provider in providers:
        try:
            bag = provider(event)
        except Exception:
            continue  # never-raise: a broken provider must never break capture
        if not bag:
            continue
        for key, value in bag.items():
            if key not in meta:
                meta[key] = value


class _Probe:
    """A throwaway event stand-in, so :func:`ambient_attrs` reuses :func:`apply_ambient`'s merge
    (registration order, never-overwrite, never-raise) instead of duplicating it."""

    __slots__ = ("metadata",)

    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}


def ambient_attrs() -> dict[str, Any]:
    """What the registered providers would stamp **right now** — for a consumer with no event.

    ``apply_ambient`` covers everything that *is* an event (an ``LLMCall``, a ``ToolCall``). A
    governance record is not: an audit entry or an enforcement decision is built by ``acttrace`` /
    ``tokenguard`` / ``guardrails``, which must not import the SDK (rule 2) and so had no way to
    learn which agent was acting. Measured 2026-07-26: **13 of 386** SDK governance rows named their
    agent, so "which agent was blocked" was answerable only by inferring it from step ordering — on
    a governance product, the attribute most worth having.

    This is a **read** of the same registry, not new state: core still carries no identity of its
    own (the locked core-identity principle) — the app or the SDK registers a provider, core merges
    what it returns. Zero-provider fast path is a single length check.

    ```python
    from cendor.core import ambient_attrs
    ambient_attrs().get("agent")   # the acting agent, when something registered one
    ```
    """
    if not _providers:
        return {}
    probe = _Probe()
    apply_ambient(probe)
    return probe.metadata


def _reset_ambient() -> None:
    """Test helper: drop every registered provider."""
    with _lock:
        _providers.clear()
