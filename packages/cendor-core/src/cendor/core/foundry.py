"""Optional Azure AI Foundry Agents correlation adapter — sourcing agent + conversation id.

Azure AI Foundry Agents runs a model **server-side**: your app calls
``client.runs.create(thread_id, agent_id=…)`` (or ``create_and_process`` / ``stream``) and the run
runs on Azure. The wire calls ``instrument()`` wraps (chat/responses/embeddings/converse) never see
that run, so there is **no per-step token/cost to capture here** — this is a *correlation* adapter,
not a usage capture (GLR-11b1). It observes thread-run creation and, for the duration of the call,
registers a scoped ambient stamp so bus events raised in that synchronous scope carry
``metadata.agent = <agent id>`` and ``metadata.conversation_id = <thread id>``.

**Honest limit (attribution only).** Because the model runs server-side, a pure-Foundry flow raises
no instrumented model events — so it records agent/conversation *attribution* but **no tokens or
cost**. It is exact for correlating any *directly instrumented* calls you make inside a run scope,
and it is the standards-home for the agent/conversation identity a run carries.

**No import dependency.** Unlike the langchain / openai-agents adapters, this one **wraps a client
you pass in** (duck-typed on ``.runs``) — so importing ``cendor.core.foundry`` needs no Azure SDK.
The optional extra just installs the SDK so you have a client to wrap::

    pip install "cendor-core[foundry]"

**Never-overwrite / zero cost when unattached.** The stamp merges through core's ambient seam (an
explicit value always wins), and the provider is registered only when you attach — importing this
module registers nothing.

Usage::

    from azure.ai.agents import AgentsClient
    from cendor.core.foundry import observe_foundry_agents

    client = AgentsClient(endpoint, credential)
    observe_foundry_agents(client)          # wraps runs.create / create_and_process / stream
    run = client.runs.create_and_process(thread.id, agent_id=agent.id)  # scope carries the ids

    # or scope your own block explicitly (works without a client):
    from cendor.core.foundry import foundry_agent_scope
    with foundry_agent_scope(agent_id="asst_123", thread_id="thread_abc"):
        ...  # any instrumented call here is attributed to that agent + conversation
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .ambient import add_ambient_provider

__all__ = ["observe_foundry_agents", "foundry_agent_scope"]

#: The active Foundry run's {agent, conversation_id} for the current flow. Set by the scope context
#: manager, read by the ambient provider at event construction. ``None`` outside a run scope (a
#: mutable default would be a shared-state footgun, so it is None and reads coalesce to {}).
_active: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "cendor_foundry_active", default=None
)


def _provider(_event: Any) -> dict[str, Any] | None:
    """Ambient provider: stamp ``agent`` + ``conversation_id`` from the active Foundry run scope.
    Non-empty keys only; core's never-overwrite seam keeps any explicit value."""
    active = _active.get() or {}
    out: dict[str, Any] = {}
    if active.get("agent"):
        out["agent"] = active["agent"]
    # D3 (core 1.14.0): Foundry hands us a real, stable agent **id**, so it also rides the semconv
    # identity attribute (``gen_ai.agent.id``) rather than only the name slot. ``agent`` keeps
    # carrying it too — it has since this adapter shipped, and a dashboard grouping on the name
    # dimension must not lose its rows on an upgrade. Still **attribution-only**: mapping the
    # identity does not make Foundry's server-side tokens or cost appear (see the module docstring).
    if active.get("agent_id"):
        out["agent_id"] = active["agent_id"]
    if active.get("conversation_id"):
        out["conversation_id"] = active["conversation_id"]
    return out or None


@contextmanager
def foundry_agent_scope(
    agent_id: str | None = None, thread_id: str | None = None
) -> Iterator[None]:
    """Scope a block to a Foundry agent + conversation: registers the ambient provider (idempotent)
    and stamps ``agent`` / ``conversation_id`` for the duration. Both optional — an empty scope
    stamps nothing. Attribution-only (no server-side token/cost). Restores the prior scope on
    exit."""
    add_ambient_provider(_provider)  # idempotent — dedups by identity
    token = _active.set(
        {
            "agent": str(agent_id) if agent_id else "",
            "agent_id": str(agent_id) if agent_id else "",
            "conversation_id": str(thread_id) if thread_id else "",
        }
    )
    try:
        yield
    finally:
        _active.reset(token)


def _extract_agent(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """The agent id from a runs.create/create_and_process/stream call: the keyword-only ``agent_id``
    (Azure's name; ``assistant_id`` on some surfaces), else a dict body carrying it."""
    aid = kwargs.get("agent_id") or kwargs.get("assistant_id")
    if aid:
        return str(aid)
    for a in args:
        if isinstance(a, dict):
            v = a.get("agent_id") or a.get("assistant_id")
            if v:
                return str(v)
    return None


def _thread_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """The thread id: the first positional arg (all three methods take ``thread_id`` first), else
    the ``thread_id`` keyword."""
    if args:
        return str(args[0])
    tid = kwargs.get("thread_id")
    return str(tid) if tid else None


_RUN_METHODS = ("create", "create_and_process", "stream")


def observe_foundry_agents(client: Any) -> Any:
    """Wrap a Foundry ``AgentsClient``'s run-creation methods so each call establishes a scoped
    ambient stamp (``agent`` + ``conversation_id``) for its duration. Idempotent per method
    (re-wrapping is a no-op). Handles both the sync client and the ``.aio`` async client. Returns
    the client for chaining. Raises :class:`TypeError` if the object has no ``.runs``.

    Attribution-only — see the module docstring; the model runs server-side, so no token/cost is
    captured here. When the SDK's ``.runs`` uses ``__slots__`` and a method can't be replaced, that
    method is skipped — use :func:`foundry_agent_scope` directly (documented honest fallback)."""
    runs = getattr(client, "runs", None)
    if runs is None:
        raise TypeError(
            "observe_foundry_agents expects an Azure AI Foundry AgentsClient (with a `.runs` "
            f"operations group); got {type(client).__name__!r}. Use foundry_agent_scope(...) to "
            "scope a block manually instead."
        )
    for name in _RUN_METHODS:
        _wrap_run_method(runs, name)
    return client


def _wrap_run_method(runs: Any, name: str) -> None:
    """Replace ``runs.<name>`` with a wrapper that scopes the call to its agent + thread. No-op if
    the method is absent or already wrapped; silently skips one that can't be set
    (``__slots__``)."""
    orig = getattr(runs, name, None)
    if orig is None or getattr(orig, "_cendor_foundry_wrapped", False):
        return

    if inspect.iscoroutinefunction(orig):

        @functools.wraps(orig)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            with foundry_agent_scope(_extract_agent(args, kwargs), _thread_id(args, kwargs)):
                return await orig(*args, **kwargs)

        awrapper._cendor_foundry_wrapped = True  # type: ignore[attr-defined]
        _set(runs, name, awrapper)
    else:

        @functools.wraps(orig)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with foundry_agent_scope(_extract_agent(args, kwargs), _thread_id(args, kwargs)):
                return orig(*args, **kwargs)

        wrapper._cendor_foundry_wrapped = True  # type: ignore[attr-defined]
        _set(runs, name, wrapper)


def _set(obj: Any, name: str, value: Any) -> None:
    """setattr that tolerates a ``__slots__`` / read-only operations group (skip rather than crash —
    the manual :func:`foundry_agent_scope` remains available)."""
    try:
        setattr(obj, name, value)
    except (AttributeError, TypeError):  # pragma: no cover - depends on installed SDK internals
        pass
