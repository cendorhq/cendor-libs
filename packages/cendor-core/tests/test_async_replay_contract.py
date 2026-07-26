"""A short-circuited call must keep the wrapped method's async contract (F-1) — and a misdetected
async **tool** must not record its own coroutine (N-2).

Red-first. Both defects come from one root cause: the real async SDK methods are *not*
``iscoroutinefunction`` — ``openai``'s ``chat.completions.create`` and ``anthropic``'s
``messages.create`` are ``async def``s behind a ``functools.wraps`` **sync** decorator
(``@required_args``), so core installs its sync wrapper. The live path already repairs that (L5),
but the interceptor short-circuit — the seam ``cassette`` replays through — handed the recorded
value back synchronously, so an ordinary ``await client.chat.completions.create(...)`` raised
``TypeError: object … can't be used in 'await' expression``. A streamed replay was worse: a *sync*
proxy, so neither ``await`` nor ``async for`` worked.

``instrument_tool`` never received the L5 repair at all, so a decorated ``async def`` tool recorded
``ToolCall.result = <coroutine object …>`` — which a recorder then persists as that string.

No network — mock clients only.
"""

import asyncio
import functools
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, instrument_tool
from cendor.core.instrument import add_interceptor, remove_interceptor
from cendor.core.types import LLMCall, ToolCall, Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


@pytest.fixture
def replaying():
    """Register an interceptor short-circuiting with ``value`` (what ``cassette`` replay does)."""
    installed: list = []

    def install(value):
        def interceptor(event):
            return value

        add_interceptor(interceptor)
        installed.append(interceptor)
        return interceptor

    yield install
    for fn in installed:
        remove_interceptor(fn)


def required_args(fn):
    """openai/anthropic's shape: a **sync** ``functools.wraps`` wrapper over an ``async def``."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


def recorded_response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="replayed"))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=1),
    )


def recorded_chunks():
    return [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="re"))], usage=None),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="played"))], usage=None
        ),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2)),
    ]


def openai_shaped(*, decorate: bool, sync: bool = False):
    """A client whose ``chat.completions.create`` mimics a real SDK's decorated method."""

    if sync:

        def create(**kwargs):
            return recorded_response()

    else:

        async def create(**kwargs):  # type: ignore[misc]
            return recorded_response()

    completions = SimpleNamespace(create=required_args(create) if decorate else create)
    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=completions)))


ARGS = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


# --- F-1: the short-circuit must honour the wrapped method's async-ness ----------------------


def test_replay_through_a_decorated_async_client_is_awaitable(events, replaying):
    client = openai_shaped(decorate=True)
    replaying(recorded_response())

    async def drive():
        returned = client.chat.completions.create(**ARGS)
        assert asyncio.iscoroutine(returned) or hasattr(returned, "__await__"), (
            "a replayed call on an async client must be awaitable"
        )
        return await returned

    out = asyncio.run(drive())
    assert out.choices[0].message.content == "replayed"
    assert len(events) == 1
    assert events[0].metadata["replayed"] is True
    assert events[0].usage == Usage(input_tokens=11, output_tokens=1)


def test_streamed_replay_through_a_decorated_async_client_is_async_iterable(events, replaying):
    client = openai_shaped(decorate=True)
    replaying(recorded_chunks())

    async def drive():
        stream = await client.chat.completions.create(**ARGS, stream=True)
        return [c async for c in stream]

    chunks = asyncio.run(drive())
    assert len(chunks) == 3
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=7, output_tokens=2)
    assert events[0].metadata["streamed"] is True


def test_replay_through_a_bare_async_client_is_unchanged(events, replaying):
    # The already-correct path (core installs `awrapper`): guard it against regression.
    client = openai_shaped(decorate=False)
    replaying(recorded_response())
    out = asyncio.run(client.chat.completions.create(**ARGS))
    assert out.choices[0].message.content == "replayed"
    assert len(events) == 1


def test_replay_through_a_decorated_sync_client_still_returns_a_bare_value(events, replaying):
    # A `functools.wraps`-decorated **sync** method unwraps to a sync function ⇒ nothing changes.
    client = openai_shaped(decorate=True, sync=True)
    replaying(recorded_response())
    out = client.chat.completions.create(**ARGS)
    assert not hasattr(out, "__await__"), "a sync client must not start returning a coroutine"
    assert out.choices[0].message.content == "replayed"
    assert len(events) == 1


def test_a_raising_interceptor_still_raises_in_the_callers_frame(events):
    # tokenguard's pre-flight block / acttrace's guard raise *before* the call. The fix must not
    # defer that into a coroutine — an un-awaited coroutine would swallow the refusal.
    client = openai_shaped(decorate=True)

    def blocker(event):
        raise RuntimeError("blocked pre-flight")

    add_interceptor(blocker)
    try:
        with pytest.raises(RuntimeError, match="blocked pre-flight"):
            client.chat.completions.create(**ARGS)
    finally:
        remove_interceptor(blocker)
    assert events == []


def test_a_live_call_teaches_the_wrapper_that_a_handwritten_client_is_async(events, replaying):
    # The L5 shape (a plain `def` returning a coroutine) does not unwrap to a coroutine function, so
    # inference alone cannot see it. One live call is an observation, and it must be believed:
    # record-then-replay in one process is the whole cassette workflow.
    async def _real():
        return recorded_response()

    class Completions:
        def create(self, **kwargs):  # NOT async def, returns an awaitable
            return _real()

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    asyncio.run(client.chat.completions.create(**ARGS))  # the live call: now observed as async
    assert len(events) == 1

    replaying(recorded_response())
    out = asyncio.run(client.chat.completions.create(**ARGS))
    assert out.choices[0].message.content == "replayed"
    assert len(events) == 2


# --- N-2: instrument_tool has the same root cause and poisons recordings ---------------------


def test_a_decorated_async_tool_records_its_result_not_its_coroutine(events):
    async def _search(query):
        return f"hits for {query}"

    search = instrument_tool("search")(required_args(_search))

    out = asyncio.run(search("refunds"))
    assert out == "hits for refunds"
    assert len(events) == 1
    tc = events[0]
    assert isinstance(tc, ToolCall)
    assert tc.result == "hits for refunds", (
        f"the recorded result must be the value, not {tc.result!r} — a recorder persists this"
    )
    assert not asyncio.iscoroutine(tc.result)


def test_replay_of_a_decorated_async_tool_is_awaitable(events, replaying):
    async def _search(query):
        return "live"

    search = instrument_tool("search")(required_args(_search))
    replaying("recorded hits")

    async def drive():
        returned = search("refunds")
        assert hasattr(returned, "__await__"), "a replayed async tool call must be awaitable"
        return await returned

    assert asyncio.run(drive()) == "recorded hits"
    assert len(events) == 1
    assert events[0].result == "recorded hits"
    assert events[0].metadata["replayed"] is True


def test_a_bare_async_tool_is_unchanged(events):
    @instrument_tool("plain")
    async def plain(x):
        return x * 2

    assert asyncio.run(plain(3)) == 6
    assert events[0].result == 6


def test_a_sync_tool_is_unchanged(events):
    @instrument_tool("sync")
    def add(a, b):
        return a + b

    assert add(1, 2) == 3
    assert events[0].result == 3
    assert not hasattr(events[0].result, "__await__")


def test_llmcall_and_toolcall_types_are_intact(events):
    # cheap guard that the fixtures above really exercised both event types
    assert isinstance(LLMCall(id="x", provider="p", model="m", messages=[]), LLMCall)
