"""Provider-native agent identity (W4/D3) — map an id that exists, invent nothing.

Measured 2026-07-26 (report §6.1): `gen_ai.agent.id` was never emitted and never stored, and the
provider-native ids that DO exist — Foundry's `agent_id`/`thread_id`, Bedrock Agents'
`agentId`/`sessionId`, an OpenAI `assistant_id` — were dropped on the floor. So the answer to "do we
get an agent id from the provider" was "no" even for the products that hand one over.

The rails here: the mapping lives in an **adapter** (core carries no identity of its own, and there
is no `CENDOR_AGENT_NAME`), an absent id means the attribute is **omitted** rather than invented,
and two concurrent flows never cross-attribute.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.core import ambient_attrs, bus, instrument
from cendor.core.agent_ids import agent_scope, bedrock_agent_scope, openai_assistant_scope
from cendor.core.ambient import _reset_ambient


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    _reset_ambient()
    yield
    bus._reset()
    _reset_ambient()


def _client():
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4))

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _capture():
    seen: list = []
    bus.subscribe(seen.append)
    return seen


def test_bedrock_agent_scope_maps_agentId_and_sessionId():
    seen = _capture()
    client = instrument(_client())
    with bedrock_agent_scope(agent_id="AGENT123", agent_alias_id="TSTALIASID", session_id="sess-7"):
        client.chat.completions.create(model="gpt-4o-mini", messages=[])
    call = seen[-1]
    # An ALIAS resolves to a version, so two aliases of one agent are different things to attribute
    # to — collapsing them would put a number against the wrong one.
    assert call.metadata["agent_id"] == "AGENT123/TSTALIASID"
    assert call.metadata["conversation_id"] == "sess-7"
    # Bedrock's invocation carries no NAME, so none is invented.
    assert "agent" not in call.metadata


def test_bedrock_without_an_alias_uses_the_bare_agent_id():
    seen = _capture()
    client = instrument(_client())
    with bedrock_agent_scope(agent_id="AGENT123", session_id="s"):
        client.chat.completions.create(model="gpt-4o-mini", messages=[])
    assert seen[-1].metadata["agent_id"] == "AGENT123"


def test_openai_assistant_scope_maps_assistant_id_and_thread():
    seen = _capture()
    client = instrument(_client())
    with openai_assistant_scope(assistant_id="asst_abc", thread_id="thread_xyz", name="Billing"):
        client.chat.completions.create(model="gpt-4o-mini", messages=[])
    call = seen[-1]
    assert call.metadata["agent_id"] == "asst_abc"
    assert call.metadata["conversation_id"] == "thread_xyz"
    assert call.metadata["agent"] == "Billing"


def test_an_empty_scope_stamps_NOTHING():
    """The whole point of D3: absent identity is absent, not a hash and not a placeholder."""
    seen = _capture()
    client = instrument(_client())
    with agent_scope():
        client.chat.completions.create(model="gpt-4o-mini", messages=[])
    meta = seen[-1].metadata
    for key in ("agent", "agent_id", "conversation_id"):
        assert key not in meta, f"{key} was invented for an empty scope: {meta}"


def test_a_call_outside_a_scope_carries_no_identity():
    seen = _capture()
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o-mini", messages=[])
    assert "agent_id" not in seen[-1].metadata


def test_the_scope_restores_the_previous_one_on_exit():
    seen = _capture()
    client = instrument(_client())
    with agent_scope(name="outer", agent_id="id-outer"):
        with agent_scope(name="inner", agent_id="id-inner"):
            client.chat.completions.create(model="gpt-4o-mini", messages=[])
        client.chat.completions.create(model="gpt-4o-mini", messages=[])
    assert seen[-2].metadata["agent_id"] == "id-inner"
    assert seen[-1].metadata["agent_id"] == "id-outer", "the inner scope leaked past its block"
    assert ambient_attrs().get("agent_id") in (None, ""), "identity leaked past the outer scope"


def test_concurrent_flows_do_not_cross_attribute():
    """A ContextVar, not a process-wide holder: two threads in two scopes stay separate."""
    import threading

    seen = _capture()
    client = instrument(_client())
    barrier = threading.Barrier(2)

    def work(agent_id: str) -> None:
        with agent_scope(agent_id=agent_id):
            barrier.wait()  # both scopes open at once
            client.chat.completions.create(model="gpt-4o-mini", messages=[])

    threads = [threading.Thread(target=work, args=(f"id-{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(c.metadata["agent_id"] for c in seen) == ["id-1", "id-2"]


def test_an_explicit_value_on_the_event_still_wins():
    """core's never-overwrite seam: the adapter fills gaps, it does not override the caller."""
    seen = _capture()
    from cendor.core.types import LLMCall

    with agent_scope(agent_id="from-scope"):
        call = LLMCall(id="1", provider="openai", model="m", messages=[])
        call.metadata["agent_id"] = "explicit"
        from cendor.core.ambient import apply_ambient

        apply_ambient(call)
    assert call.metadata["agent_id"] == "explicit"
    assert seen == []


def test_ambient_attrs_reads_what_a_governance_consumer_would_stamp():
    """The seam acttrace/tokenguard use for S4 — they cannot import the SDK (rule 2), so they read
    core's registry instead. Outside a scope it is empty; inside it names the actor."""
    assert ambient_attrs() == {}
    with agent_scope(name="reviewer", agent_id="rev-1"):
        attrs = ambient_attrs()
    assert attrs["agent"] == "reviewer"
    assert attrs["agent_id"] == "rev-1"
    assert ambient_attrs() == {}
