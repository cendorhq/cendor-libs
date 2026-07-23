"""Tests for the openai-agents adapter (GLR-11c, cendor.core.openai_agents).

No network and no real Runner — a fake driver mimics the SDK's hook order (await on_agent_start →
[the agent's model call constructs an event, as instrument() would] → await on_agent_end), and
asserts the scoped ambient stamp lands on the event's metadata. openai-agents itself is only needed
import the RunHooks base class; the tests importorskip it so a plain install still collects.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("agents", reason="openai-agents (the RunHooks base) not installed")

from cendor.core.ambient import _providers, _reset_ambient, apply_ambient  # noqa: E402
from cendor.core.openai_agents import CendorAgentHooks, _active  # noqa: E402
from cendor.core.types import LLMCall  # noqa: E402


class _FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name


def _model_call() -> LLMCall:
    """Construct an LLMCall exactly where instrument() would (the caller's sync frame) and run the
    ambient providers over it — the moment the adapter's scoped stamp must be readable."""
    call = LLMCall(id="c1", provider="openai", model="gpt-4o", messages=[])
    apply_ambient(call)
    return call


@pytest.fixture(autouse=True)
def _clean_ambient():
    _reset_ambient()
    _active["agent"] = ""
    yield
    _reset_ambient()
    _active["agent"] = ""


def test_stamps_agent_name_during_a_turn():
    async def drive() -> LLMCall:
        hooks = CendorAgentHooks()
        agent = _FakeAgent("Billing")
        await hooks.on_agent_start(None, agent)
        call = _model_call()  # the agent's model call, in the same async flow
        await hooks.on_agent_end(None, agent, "done")
        return call

    call = asyncio.run(drive())
    assert call.metadata.get("agent") == "Billing"


def test_handoff_re_stamps_to_the_next_agent():
    async def drive() -> tuple[LLMCall, LLMCall]:
        hooks = CendorAgentHooks()
        a, b = _FakeAgent("Triage"), _FakeAgent("Refunds")
        await hooks.on_agent_start(None, a)
        first = _model_call()
        await hooks.on_handoff(None, a, b)  # A → B
        second = _model_call()
        await hooks.on_agent_end(None, b, "done")
        return first, second

    first, second = asyncio.run(drive())
    assert first.metadata.get("agent") == "Triage"
    assert second.metadata.get("agent") == "Refunds"


def test_scope_cleared_after_agent_end():
    async def drive() -> LLMCall:
        hooks = CendorAgentHooks()
        agent = _FakeAgent("Billing")
        await hooks.on_agent_start(None, agent)
        await hooks.on_agent_end(None, agent, "done")
        return _model_call()  # after the turn ends — no active agent

    call = asyncio.run(drive())
    assert "agent" not in call.metadata


def test_never_overwrites_an_explicit_agent():
    async def drive() -> LLMCall:
        hooks = CendorAgentHooks()
        await hooks.on_agent_start(None, _FakeAgent("Billing"))
        call = LLMCall(id="c1", provider="openai", model="gpt-4o", messages=[])
        call.metadata["agent"] = "explicit"  # an SDK scope / user stamp already present
        apply_ambient(call)
        return call

    call = asyncio.run(drive())
    assert call.metadata["agent"] == "explicit", (
        "core's never-overwrite seam keeps the explicit value"
    )


def test_import_registers_nothing_construction_registers_once():
    # After a clean reset, merely having imported the module has registered no provider.
    assert len(_providers) == 0, "importing the adapter must not register an ambient provider"
    CendorAgentHooks()
    assert len(_providers) == 1, "constructing the hooks registers the single provider"
    CendorAgentHooks()  # idempotent — a second instance does not add a second provider
    assert len(_providers) == 1


def test_sequential_runs_each_stamp_their_own_agent():
    # The active agent is a process-wide holder (the SDK isolates the model-call context from the
    # hooks, so a contextvar can't reach the call — verified live). Sequential runs (the common
    # case) each stamp correctly because on_agent_end clears the holder between them.
    hooks = CendorAgentHooks()

    async def one(name: str) -> LLMCall:
        await hooks.on_agent_start(None, _FakeAgent(name))
        call = _model_call()
        await hooks.on_agent_end(None, _FakeAgent(name), "done")
        return call

    async def drive() -> list[LLMCall]:
        first = await one("Alpha")
        # holder cleared by Alpha's on_agent_end — a call between runs carries no agent
        between = _model_call()
        second = await one("Beta")
        return [first, between, second]

    first, between, second = asyncio.run(drive())
    assert first.metadata.get("agent") == "Alpha"
    assert "agent" not in between.metadata, "holder cleared between sequential runs"
    assert second.metadata.get("agent") == "Beta"


def test_holder_is_process_wide_documented_limit():
    # Honest-limit doc test: the holder is process-wide (NOT context-scoped), so a concurrent second
    # run's on_agent_start is visible to the first run's next call. This is the documented limit —
    # per-run scoping is impossible because the SDK isolates the model-call context from the hooks.
    hooks = CendorAgentHooks()

    async def drive() -> str | None:
        await hooks.on_agent_start(None, _FakeAgent("Alpha"))
        await hooks.on_agent_start(
            None, _FakeAgent("Beta")
        )  # a concurrent run's start, interleaved
        call = (
            _model_call()
        )  # Alpha's call now reads Beta (cross-attribution) — the documented limit
        return call.metadata.get("agent")

    assert asyncio.run(drive()) == "Beta", (
        "process-wide holder — concurrent overlap cross-attributes"
    )
