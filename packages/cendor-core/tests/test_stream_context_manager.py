"""Streamed responses are *both* an iterator and a context manager (WS-B).

The provider SDK's streamed value supports ``for chunk in stream`` **and**
``with client…create(stream=True) as stream:``. The proxy must preserve both, finalize the
``LLMCall`` exactly once, forward the SDK surface (``.response``, ``.close()``), and apply the same
to replayed streams. No network. See docs/core.md §6.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.instrument import MISS, add_interceptor, remove_interceptor
from cendor.core.types import LLMCall, Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def _chunk(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


def _usage_chunk(prompt, completion):
    return SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    )


class _FakeSDKStream:
    """Mimics an OpenAI SDK ``Stream``: iterator + context manager + ``close()`` + ``.response``."""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self.response = "RAW_RESPONSE"
        self.entered = False
        self.exited = False
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        self.close()
        return False

    def close(self):
        self.closed = True


def _client_returning(stream):
    class Completions:
        def create(self, **kwargs):
            return stream

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


# --- sync context-manager form ---------------------------------------------------------------


def test_sync_with_statement_captures_usage_once(events):
    chunks = [_chunk("Hel"), _chunk("lo"), _usage_chunk(100, 50)]
    raw = _FakeSDKStream(chunks)
    client = _client_returning(raw)

    with client.chat.completions.create(model="gpt-4o", messages=[], stream=True) as stream:
        got = list(stream)

    assert got == chunks  # every chunk passed through unchanged
    assert len(events) == 1  # exactly one LLMCall despite iterate-then-__exit__
    call = events[0]
    assert isinstance(call, LLMCall)
    assert call.metadata["streamed"] is True
    assert call.usage == Usage(input_tokens=100, output_tokens=50)
    assert call.cost.amount == Decimal("0.00075")
    assert raw.entered and raw.exited and raw.closed  # the SDK stream's own CM ran + it was closed


def test_with_on_plain_iterator_still_works(events):
    # An underlying without its own CM (a bare iterator) must still support `with` via the wrapper.
    chunks = [_chunk("x"), _usage_chunk(4, 1)]

    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    with client.chat.completions.create(model="gpt-4o", messages=[], stream=True) as stream:
        got = list(stream)

    assert got == chunks
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=4, output_tokens=1)


def test_with_statement_single_finalize_without_full_iteration(events):
    chunks = [_chunk("a"), _chunk("b"), _usage_chunk(5, 5)]
    raw = _FakeSDKStream(chunks)
    client = _client_returning(raw)

    with client.chat.completions.create(model="gpt-4o", messages=[], stream=True) as stream:
        next(stream)  # consume just one chunk, then leave the block early

    assert len(events) == 1  # __exit__ finalizes once even without exhausting the stream
    assert events[0].metadata["streamed"] is True
    assert raw.closed


def test_getattr_forwards_sdk_surface(events):
    raw = _FakeSDKStream([_usage_chunk(1, 1)])
    client = _client_returning(raw)
    stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)

    assert stream.response == "RAW_RESPONSE"  # unknown attr forwarded to the underlying SDK stream
    list(stream)


def test_close_finalizes_once_and_closes_underlying(events):
    raw = _FakeSDKStream([_chunk("a"), _usage_chunk(5, 5)])
    client = _client_returning(raw)
    stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)

    next(stream)
    stream.close()
    stream.close()  # idempotent — a second close must not double-emit

    assert raw.closed
    assert len(events) == 1


def test_iterate_then_context_exit_does_not_double_emit(events):
    raw = _FakeSDKStream([_chunk("a"), _usage_chunk(2, 2)])
    client = _client_returning(raw)
    stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)

    with stream:
        list(stream)  # exhaustion finalizes...
    # ...and __exit__ would finalize again, but the _finalized flag makes it a no-op.
    assert len(events) == 1


# --- async context-manager form --------------------------------------------------------------


class _FakeAsyncSDKStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._i = 0
        self.entered = False
        self.exited = False
        self.closed = False
        self.response = "RAW_RESPONSE"

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._i]
        self._i += 1
        return c

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        await self.close()
        return False

    async def close(self):
        self.closed = True


def _async_client_returning(stream):
    class Completions:
        async def create(self, **kwargs):
            return stream

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


async def test_async_with_statement_captures_usage_once(events):
    chunks = [_chunk("Hi"), _usage_chunk(10, 5)]
    raw = _FakeAsyncSDKStream(chunks)
    client = _async_client_returning(raw)

    stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    async with stream as s:
        got = [c async for c in s]

    assert got == chunks
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=10, output_tokens=5)
    assert raw.entered and raw.exited and raw.closed


async def test_async_aclose_finalizes_once(events):
    chunks = [_chunk("a"), _usage_chunk(3, 3)]
    raw = _FakeAsyncSDKStream(chunks)
    client = _async_client_returning(raw)

    stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    await stream.__anext__()
    await stream.aclose()

    assert raw.closed
    assert len(events) == 1


# --- replay parity: a replayed stream must also support `with` --------------------------------


def test_replay_stream_supports_with(events):
    recorded = [_chunk("re"), _chunk("play"), _usage_chunk(7, 3)]

    class Completions:
        def create(self, **kwargs):  # never runs — the interceptor short-circuits it
            raise AssertionError("real create() must not run on replay")

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    def replayer(event):
        return recorded if isinstance(event, LLMCall) else MISS

    add_interceptor(replayer)
    try:
        with client.chat.completions.create(model="gpt-4o", messages=[], stream=True) as stream:
            got = list(stream)
    finally:
        remove_interceptor(replayer)

    assert got == recorded
    assert len(events) == 1
    assert events[0].metadata["replayed"] is True
    assert events[0].usage == Usage(input_tokens=7, output_tokens=3)


async def test_async_replay_stream_supports_async_with(events):
    recorded = [_chunk("a"), _usage_chunk(4, 4)]

    class Completions:
        async def create(self, **kwargs):
            raise AssertionError("real create() must not run on replay")

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    def replayer(event):
        return recorded if isinstance(event, LLMCall) else MISS

    add_interceptor(replayer)
    try:
        stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        async with stream as s:
            got = [c async for c in s]
    finally:
        remove_interceptor(replayer)

    assert got == recorded
    assert len(events) == 1
    assert events[0].metadata["replayed"] is True
    assert events[0].usage == Usage(input_tokens=4, output_tokens=4)
