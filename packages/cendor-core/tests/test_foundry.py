"""Tests for the Azure AI Foundry correlation adapter (GLR-11b1, cendor.core.foundry).

No network and no Azure SDK — the adapter wraps a client by duck-typing on ``.runs``, so fake
clients whose run methods construct a bus event (as an instrumented call inside the run scope would)
assert the scoped agent + conversation stamp lands. Attribution-only: the real model runs
server-side, so this only carries ids — which is exactly what these tests verify.
"""

from __future__ import annotations

import asyncio

import pytest
from cendor.core.ambient import _providers, _reset_ambient, apply_ambient
from cendor.core.foundry import foundry_agent_scope, observe_foundry_agents
from cendor.core.types import LLMCall


def _call_in_scope() -> LLMCall:
    call = LLMCall(id="c1", provider="azure", model="gpt-4o", messages=[])
    apply_ambient(call)
    return call


class _FakeRuns:
    def create(self, thread_id, *, agent_id, **kw):  # mirrors azure RunsOperations.create
        return _call_in_scope()

    def create_and_process(self, thread_id, *, agent_id, **kw):
        return _call_in_scope()

    def stream(self, thread_id, *, agent_id, **kw):
        return _call_in_scope()


class _FakeClient:
    def __init__(self):
        self.runs = _FakeRuns()


class _FakeAioRuns:
    async def create(self, thread_id, *, agent_id, **kw):
        return _call_in_scope()


class _FakeAioClient:
    def __init__(self):
        self.runs = _FakeAioRuns()


@pytest.fixture(autouse=True)
def _clean_ambient():
    _reset_ambient()
    yield
    _reset_ambient()


def test_scope_stamps_agent_and_conversation():
    with foundry_agent_scope(agent_id="asst_123", thread_id="thread_abc"):
        call = _call_in_scope()
    assert call.metadata.get("agent") == "asst_123"
    assert call.metadata.get("conversation_id") == "thread_abc"


def test_scope_cleared_on_exit():
    with foundry_agent_scope(agent_id="asst_123", thread_id="thread_abc"):
        pass
    after = _call_in_scope()
    assert "agent" not in after.metadata
    assert "conversation_id" not in after.metadata


def test_observe_wraps_create_and_stamps():
    client = _FakeClient()
    observe_foundry_agents(client)
    call = client.runs.create("thread_abc", agent_id="asst_9")
    assert call.metadata.get("agent") == "asst_9"
    assert call.metadata.get("conversation_id") == "thread_abc"
    # create_and_process + stream are wrapped too
    c2 = client.runs.create_and_process("thread_x", agent_id="asst_x")
    assert c2.metadata.get("agent") == "asst_x" and c2.metadata.get("conversation_id") == "thread_x"


def test_observe_wraps_async_client():
    client = _FakeAioClient()
    observe_foundry_agents(client)

    async def drive():
        return await client.runs.create("thread_async", agent_id="asst_async")

    call = asyncio.run(drive())
    assert call.metadata.get("agent") == "asst_async"
    assert call.metadata.get("conversation_id") == "thread_async"


def test_observe_is_idempotent():
    client = _FakeClient()
    observe_foundry_agents(client)
    observe_foundry_agents(client)  # re-wrap must be a no-op (no double-wrap)
    call = client.runs.create("t", agent_id="a")
    assert call.metadata.get("agent") == "a"
    # only ONE provider registered despite two observe calls + a manual scope elsewhere
    assert len(_providers) == 1


def test_never_overwrites_explicit_values():
    with foundry_agent_scope(agent_id="asst_123", thread_id="thread_abc"):
        call = LLMCall(id="c1", provider="azure", model="gpt-4o", messages=[])
        call.metadata["agent"] = "explicit"
        apply_ambient(call)
    assert call.metadata["agent"] == "explicit"
    # conversation_id was not pre-set, so the scope still supplies it
    assert call.metadata.get("conversation_id") == "thread_abc"


def test_import_registers_nothing_until_attached():
    assert len(_providers) == 0, "importing the adapter registers no provider"
    with foundry_agent_scope(agent_id="a", thread_id="t"):
        pass
    assert len(_providers) == 1, "entering a scope registers the single provider"


def test_observe_rejects_non_client():
    with pytest.raises(TypeError):
        observe_foundry_agents(object())
