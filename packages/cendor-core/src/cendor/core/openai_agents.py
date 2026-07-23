"""Optional openai-agents (OpenAI Agents SDK) integration — sourcing the framework's agent name.

The OpenAI Agents SDK (``openai-agents``, imported as ``agents``) runs its own agent loop and
exposes a lifecycle-hooks surface (:class:`agents.RunHooks`). :class:`CendorAgentHooks` is a
``RunHooks`` subclass you pass to ``Runner.run(..., hooks=CendorAgentHooks())``: on each turn it
stamps the **framework's** agent name onto a scoped ambient provider, so every bus event in the
turn carries ``metadata.agent``. The agent's model calls ride the standard OpenAI client — which
``instrument()`` already wraps — so **tokens, cost, and streaming come for free**; this adapter
supplies *only* the name (GLR-11c). It mirrors
:class:`~cendor.core.langchain.CendorCallbackHandler` (GLR-11a): the framework owns agent identity;
cendor-core carries it onto the bus.

**Never-overwrite.** The name is merged through core's ambient seam, which never overwrites a key
already present — so an explicit stamp (an SDK scope, a user ``add_ambient_provider``, an
``instrument()`` metadata) always wins.

**Zero cost when unattached.** Importing this module registers nothing; the single ambient provider
is registered the first time you construct :class:`CendorAgentHooks`. If you never attach the hooks,
core's zero-provider fast path is untouched.

**Honest limit.** The name is scoped per active agent turn (set at agent start / handoff, cleared at
agent end). Handoffs — the SDK's primary multi-agent model — re-stamp correctly. A deeply nested
*agent-as-tool* stamps the innermost active agent for its turn; when it ends the stamp clears rather
than restoring the parent — the common single-agent + handoff flows are exact.

Requires the optional extra, keeping ``cendor-core`` dependency-light (like ``[langchain]``)::

    pip install "cendor-core[openai-agents]"

Importing this module without ``openai-agents`` installed raises a clear :class:`ImportError`.

Usage::

    from agents import Agent, Runner
    from cendor.core.openai_agents import CendorAgentHooks
    from cendor.core import instrument
    from openai import AsyncOpenAI

    instrument(AsyncOpenAI())  # tokens/cost/streaming — the agent's calls ride this client

    agent = Agent(name="Billing", instructions="…")
    await Runner.run(agent, "refund my order", hooks=CendorAgentHooks())  # events carry the name
"""

from __future__ import annotations

import contextvars
from typing import Any

from .ambient import add_ambient_provider

try:
    from agents import RunHooks as _RunHooks
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "cendor.core.openai_agents requires openai-agents (imported as `agents`). "
        'Install it with:  pip install "cendor-core[openai-agents]"'
    ) from exc

__all__ = ["CendorAgentHooks"]

#: The agent currently executing a turn in the active async flow — set by the hooks and read by the
#: ambient provider at every event's construction site. Empty outside a run. A plain ``.set`` (no
#: token/reset) is concurrency-safe: ``asyncio.gather`` / task creation copies the context per run,
#: so concurrent runs sharing one hooks instance never clobber each other; mirrors the shipped SDK
#: ``_governance._active_agent`` pattern.
_active_agent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cendor_openai_agents_active_agent", default=""
)


def _provider(_event: Any) -> dict[str, Any] | None:
    """Ambient provider: stamp ``agent`` from the active-turn contextvar. Non-empty only; core's
    never-overwrite seam keeps any explicit value."""
    name = _active_agent.get()
    return {"agent": name} if name else None


class CendorAgentHooks(_RunHooks):  # type: ignore[misc]  # _RunHooks is generic; no type param
    """A ``RunHooks`` for the OpenAI Agents SDK that stamps the framework's agent name onto cendor's
    bus for the duration of each agent turn. Pass it to ``Runner.run(agent, input, hooks=…)``.

    The agent's model calls go through the standard OpenAI client, so ``instrument()`` captures
    their tokens/cost/streaming; this only adds ``metadata.agent``. Recording-only, never-overwrite,
    and exception-safe (a name recorder must never break a run).

    Example::

        from cendor.core.openai_agents import CendorAgentHooks
        await Runner.run(agent, "hi", hooks=CendorAgentHooks())
    """

    def __init__(self) -> None:
        super().__init__()
        # Register the single ambient provider on attach (idempotent — add_ambient_provider dedups
        # by identity). Merely importing this module registers nothing, so core's zero-provider fast
        # path stays intact until you actually construct the hooks (AAI-D3).
        add_ambient_provider(_provider)

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        """Stamp the starting agent's name for its turn (GLR-11c)."""
        try:
            name = getattr(agent, "name", None)
            if name:
                _active_agent.set(str(name))
        except Exception:  # noqa: BLE001 - a recorder must never break the run
            pass

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        """Re-stamp to the agent being handed off to (the SDK's multi-agent model)."""
        try:
            name = getattr(to_agent, "name", None)
            if name:
                _active_agent.set(str(name))
        except Exception:  # noqa: BLE001
            pass

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        """Clear the stamp when the agent turn ends."""
        try:
            _active_agent.set("")
        except Exception:  # noqa: BLE001
            pass
