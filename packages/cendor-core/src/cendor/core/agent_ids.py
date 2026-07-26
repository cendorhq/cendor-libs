"""Provider-native agent identity — scopes that map an *existing* id onto the semconv attributes.

**Nothing here invents identity.** A chat-completions response carries no agent identity at all, so
for a plain model call the honest answer stays "there is none". But three products *do* own a real,
stable agent id, and Cendor was dropping all three on the floor (measured 2026-07-26, report §6.1):

* **AWS Bedrock Agents** — ``agentId`` (+ ``agentAliasId``) and ``sessionId``
  → ``gen_ai.agent.id`` · ``gen_ai.conversation.id``
* **OpenAI Assistants** — ``assistant_id`` and the thread id
  → ``gen_ai.agent.id`` · ``gen_ai.conversation.id``
* **Azure AI Foundry Agent Service** — ``agent_id`` / ``thread_id``; see :mod:`cendor.core.foundry`

Each is an **adapter**, exactly like :mod:`cendor.core.foundry` and
:mod:`cendor.core.openai_agents`: the framework owns the identity, the adapter forwards it, and
``cendor-core`` itself still carries no agent or app identity of its own (the locked
core-identity principle — there is no
``CENDOR_AGENT_NAME``, and there never will be).

**Attribution-only, and the limit is the point.** These scopes attribute the calls made inside them.
They do **not** make a server-side runtime's tokens or cost appear: when the agent loop runs on the
provider's side, no model call passes through ``instrument()``, so there is nothing to price. Wrap
your own instrumented calls in the scope and you get both; wrap a purely server-side run and you get
identity without usage. Anything else would be a fabricated number.

```python
from cendor.core import instrument
from cendor.core.agent_ids import bedrock_agent_scope

client = instrument(boto3.client("bedrock-runtime"))
with bedrock_agent_scope(agent_id="AGENT123", agent_alias_id="TSTALIASID", session_id="sess-7"):
    ...  # every event in here carries the agent id + the conversation id
```
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .ambient import add_ambient_provider

__all__ = ["agent_scope", "bedrock_agent_scope", "openai_assistant_scope"]

#: The active scope's identity for the current flow. A ContextVar (not a process-wide holder), so
#: two concurrent flows never cross-attribute — the shape :mod:`cendor.core.foundry` already uses.
#: ``None`` outside a scope: a mutable default would be shared state.
_active: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "cendor_agent_ids_active", default=None
)


def _provider(_event: Any) -> dict[str, Any] | None:
    """Ambient provider: stamp the active scope's identity. Non-empty keys only; core's
    never-overwrite seam keeps any value the caller set explicitly."""
    active = _active.get() or {}
    out = {k: v for k, v in active.items() if v}
    return out or None


@contextmanager
def agent_scope(
    *,
    name: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> Iterator[None]:
    """Attribute everything in the block to an agent you already have identity for.

    The generic form the product-specific scopes below are built on — use it for a framework Cendor
    has no named adapter for. Every argument is optional and an empty scope stamps nothing; an
    absent id means the attribute is **omitted**, never hashed or placeholdered.

    Args:
        name: The agent's human-facing name → ``gen_ai.agent.name``.
        agent_id: Its stable id → ``gen_ai.agent.id``. A name is a label (two apps can share one,
            and a rename loses history); an id is identity.
        conversation_id: The thread/session id the run belongs to → ``gen_ai.conversation.id``. Only
            ever a real id the framework already holds — never synthesised.
    """
    add_ambient_provider(_provider)  # idempotent — dedups by identity
    token = _active.set(
        {
            "agent": str(name) if name else "",
            "agent_id": str(agent_id) if agent_id else "",
            "conversation_id": str(conversation_id) if conversation_id else "",
        }
    )
    try:
        yield
    finally:
        _active.reset(token)


@contextmanager
def bedrock_agent_scope(
    *,
    agent_id: str | None = None,
    agent_alias_id: str | None = None,
    session_id: str | None = None,
    name: str | None = None,
) -> Iterator[None]:
    """Scope a block to an **AWS Bedrock Agents** invocation.

    ``agentId`` → ``gen_ai.agent.id`` and ``sessionId`` → ``gen_ai.conversation.id``. With an alias
    the id becomes ``"<agentId>/<agentAliasId>"``: an alias is what actually resolves to a version,
    so two aliases of one agent are genuinely different things to attribute to — collapsing them
    would report a number about the wrong thing.

    Bedrock's own name for the agent is not on the invocation, so pass ``name=`` if you want a label
    beside the id; without it only the id is emitted.

    ```python
    with bedrock_agent_scope(agent_id="AGENT123", agent_alias_id="TSTALIASID", session_id="s-1"):
        client.invoke_agent(...)
    ```
    """
    full = f"{agent_id}/{agent_alias_id}" if agent_id and agent_alias_id else agent_id
    with agent_scope(name=name, agent_id=full, conversation_id=session_id):
        yield


@contextmanager
def openai_assistant_scope(
    *, assistant_id: str | None = None, thread_id: str | None = None, name: str | None = None
) -> Iterator[None]:
    """Scope a block to an **OpenAI Assistants** run: ``assistant_id`` → ``gen_ai.agent.id``,
    ``thread_id`` → ``gen_ai.conversation.id``.

    ```python
    with openai_assistant_scope(assistant_id="asst_abc", thread_id="thread_xyz"):
        client.beta.threads.runs.create(...)
    ```
    """
    with agent_scope(name=name, agent_id=assistant_id, conversation_id=thread_id):
        yield
