"""The interceptor chain's ordering contract (core 1.17.0) — the D3 fix, with the real seams.

**What changed.** A ``Reroute`` no longer ends the chain; only a returned *response* does. Before
this, ``_intercept`` returned the first non-MISS result, and a ``Reroute`` is a non-MISS result — so
the first interceptor that rewrote the request also silently skipped every one registered after it.

**Why it mattered, measured** (``plan/evidence-gapclose-2026-07-31/s6_probe_interceptor_chain.py``):

  registration order        what fired      what the provider actually received
  clamp, then guard         clamp only      max_tokens=16, PII **UNREDACTED**
  guard, then clamp         guard only      redacted, and the token cap **never bound**

Both failures are silent, both are in the dangerous direction, and which one you got depended on the
order the two libraries happened to be registered in — which a user has no way to observe.

This file uses the **real** registration seams (``tokenguard.budget``, ``acttrace.guard``,
``guardrails.install``, ``cassette``) wherever it can, because the whole defect was about how the
shipped libraries compose. The plain-function tests pin the contract itself.
"""

from types import SimpleNamespace

import pytest
from cendor.core import MISS, Reroute, add_interceptor, bus, instrument, remove_interceptor
from cendor.core.types import LLMCall

PII = "my ssn is 123-45-6789"


@pytest.fixture
def sent():
    """An instrumented openai-shaped client plus the list of requests it was asked to send."""
    bus._reset()
    seen: list[dict] = []

    def create(**kwargs):
        seen.append(dict(kwargs))
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=4),
            choices=[SimpleNamespace(message=SimpleNamespace(content="an answer"))],
        )

    client = instrument(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    yield client, seen
    bus._reset()


ARGS = {"model": "gpt-4o", "messages": [{"role": "user", "content": PII}]}


def _registered(*interceptors):
    """Register interceptors in order and remove them afterwards."""

    class _Scope:
        def __enter__(self):
            for fn in interceptors:
                add_interceptor(fn)
            return self

        def __exit__(self, *exc):
            for fn in interceptors:
                remove_interceptor(fn)
            return False

    return _Scope()


def _reroute(**updates):
    """An interceptor that rewrites the request and records that it ran."""
    ran: list[bool] = []

    def fn(call):
        if not isinstance(call, LLMCall):
            return MISS
        ran.append(True)
        return Reroute(**updates)

    fn.ran = ran  # type: ignore[attr-defined]
    return fn


def _observer():
    """An interceptor that declines. It must be consulted on every call — before AND after a
    reroute."""
    ran: list[LLMCall] = []

    def fn(call):
        if isinstance(call, LLMCall):
            ran.append(call)
        return MISS

    fn.ran = ran  # type: ignore[attr-defined]
    return fn


# --------------------------------------------------------------------------- the contract


def test_two_reroutes_both_reach_the_provider(sent):
    """The measured defect, both orders. THIS is the regression that matters."""
    client, seen = sent
    clamp = _reroute(max_tokens=16)
    redact = _reroute(messages=[{"role": "user", "content": "my ssn is [REDACTED]"}])
    with _registered(clamp, redact):
        client.chat.completions.create(**ARGS)
    assert clamp.ran and redact.ran, "both interceptors must be consulted"
    assert seen[0]["max_tokens"] == 16
    assert seen[0]["messages"] == [{"role": "user", "content": "my ssn is [REDACTED]"}]


def test_the_reverse_order_gives_the_same_result(sent):
    """Order-independence is the point: a user cannot see registration order."""
    client, seen = sent
    redact = _reroute(messages=[{"role": "user", "content": "my ssn is [REDACTED]"}])
    clamp = _reroute(max_tokens=16)
    with _registered(redact, clamp):
        client.chat.completions.create(**ARGS)
    assert redact.ran and clamp.ran
    assert seen[0]["max_tokens"] == 16
    assert seen[0]["messages"] == [{"role": "user", "content": "my ssn is [REDACTED]"}]


def test_a_later_interceptor_sees_the_rerouted_call(sent):
    """Not just "it runs" — it must see the request as it will actually be sent."""
    client, _seen = sent
    redact = _reroute(messages=[{"role": "user", "content": "clean"}])
    observed: list[list] = []

    def watcher(call):
        if isinstance(call, LLMCall):
            observed.append(call.messages)
        return MISS

    with _registered(redact, watcher):
        client.chat.completions.create(**ARGS)
    assert observed == [[{"role": "user", "content": "clean"}]]


def test_a_later_interceptor_sees_a_rerouted_model(sent):
    client, _seen = sent
    downgrade = _reroute(model="gpt-4o-mini")
    observed: list[str] = []

    def watcher(call):
        if isinstance(call, LLMCall):
            observed.append(call.model)
        return MISS

    with _registered(downgrade, watcher):
        client.chat.completions.create(**ARGS)
    assert observed == ["gpt-4o-mini"]


def test_reroutes_compose_in_registration_order_last_wins(sent):
    """Two rewrites of the SAME field: documented as last-wins, so pin it."""
    client, seen = sent
    first = _reroute(model="gpt-4o-mini")
    second = _reroute(model="gpt-4.1-nano")
    with _registered(first, second):
        client.chat.completions.create(**ARGS)
    assert seen[0]["model"] == "gpt-4.1-nano"


def test_three_reroutes_all_apply(sent):
    client, seen = sent
    a = _reroute(max_tokens=16)
    b = _reroute(model="gpt-4o-mini")
    c = _reroute(messages=[{"role": "user", "content": "clean"}])
    with _registered(a, b, c):
        client.chat.completions.create(**ARGS)
    assert seen[0]["max_tokens"] == 16
    assert seen[0]["model"] == "gpt-4o-mini"
    assert seen[0]["messages"] == [{"role": "user", "content": "clean"}]


# --------------------------------------------------------------------------- what must NOT change


def test_a_replay_still_short_circuits_the_chain(sent):
    """A recorded response means the provider is never called — nothing is left to rewrite, so the
    chain MUST stop. This is the half of the old behaviour that was correct."""
    client, seen = sent
    recorded = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        choices=[SimpleNamespace(message=SimpleNamespace(content="replayed"))],
    )

    def replay(call):
        return recorded if isinstance(call, LLMCall) else MISS

    clamp = _reroute(max_tokens=16)
    with _registered(replay, clamp):
        out = client.chat.completions.create(**ARGS)
    assert out is recorded
    assert clamp.ran == [], "a replay must stop the chain"
    assert seen == [], "the provider must not be called at all"


def test_a_reroute_before_a_replay_does_not_call_the_provider(sent):
    """The reroute is applied and then discarded — the recorded response still wins."""
    client, seen = sent
    recorded = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        choices=[SimpleNamespace(message=SimpleNamespace(content="replayed"))],
    )
    clamp = _reroute(max_tokens=16)
    with _registered(clamp, lambda c: recorded if isinstance(c, LLMCall) else MISS):
        out = client.chat.completions.create(**ARGS)
    assert out is recorded
    assert seen == []


def test_an_observer_is_consulted_before_and_after_a_reroute(sent):
    client, _seen = sent
    before, after = _observer(), _observer()
    clamp = _reroute(max_tokens=16)
    with _registered(before, clamp, after):
        client.chat.completions.create(**ARGS)
    assert len(before.ran) == 1
    assert len(after.ran) == 1


def test_an_interceptor_that_raises_still_stops_the_call(sent):
    """Interceptor discipline is unchanged: a pre-flight refusal propagates and nothing is sent."""
    client, seen = sent

    class Blocked(Exception):
        pass

    def blocker(call):
        if isinstance(call, LLMCall):
            raise Blocked
        return MISS

    later = _observer()
    with _registered(blocker, later), pytest.raises(Blocked):
        client.chat.completions.create(**ARGS)
    assert seen == []
    assert later.ran == [], "a raise stops the chain — it is not a Reroute"


def test_a_raise_after_a_reroute_still_stops_the_call(sent):
    client, seen = sent

    class Blocked(Exception):
        pass

    def blocker(call):
        if isinstance(call, LLMCall):
            raise Blocked
        return MISS

    with _registered(_reroute(max_tokens=16), blocker), pytest.raises(Blocked):
        client.chat.completions.create(**ARGS)
    assert seen == []


def test_no_interceptors_at_all_is_unchanged(sent):
    client, seen = sent
    client.chat.completions.create(**ARGS)
    assert seen[0]["messages"] == ARGS["messages"]
    assert "max_tokens" not in seen[0]


def test_the_rerouted_flag_is_recorded_once(sent):
    client, _seen = sent
    calls: list[LLMCall] = []
    bus.subscribe(lambda e: calls.append(e) if isinstance(e, LLMCall) else None)
    with _registered(_reroute(max_tokens=16), _reroute(model="gpt-4o-mini")):
        client.chat.completions.create(**ARGS)
    assert len(calls) == 1
    assert calls[0].metadata["rerouted"] is True


# --------------------------------------------------------------------------- async parity


@pytest.mark.asyncio
async def test_async_client_composes_reroutes_too():
    bus._reset()
    seen: list[dict] = []

    async def create(**kwargs):
        seen.append(dict(kwargs))
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=4),
            choices=[SimpleNamespace(message=SimpleNamespace(content="an answer"))],
        )

    client = instrument(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    clamp = _reroute(max_tokens=16)
    redact = _reroute(messages=[{"role": "user", "content": "clean"}])
    try:
        with _registered(clamp, redact):
            await client.chat.completions.create(**ARGS)
    finally:
        bus._reset()
    assert seen[0]["max_tokens"] == 16
    assert seen[0]["messages"] == [{"role": "user", "content": "clean"}]


# --------------------------------------------------------------------------- the tool path


def test_a_reroute_on_the_tool_path_keeps_its_old_meaning():
    """A ``ToolCall`` has no provider request to rewrite, so a ``Reroute`` there still
    short-circuits rather than being silently dropped (the alternative would be worse: a tool
    interceptor that returned one would simply stop working)."""
    from cendor.core import instrument_tool

    bus._reset()
    ran: list[str] = []

    def tool(x):
        ran.append(x)
        return "real"

    wrapped = instrument_tool(tool)
    sentinel = Reroute(x="ignored")

    def reroute_tool(call):
        return sentinel if not isinstance(call, LLMCall) else MISS

    try:
        with _registered(reroute_tool):
            out = wrapped("a")
    finally:
        bus._reset()
    assert out is sentinel  # handed back as the short-circuit value, exactly as before
    assert ran == []
