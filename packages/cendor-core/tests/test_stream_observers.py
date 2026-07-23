"""Core stream-observer seam: per-chunk hook, raise-aborts contract, zero-observer byte-identity.

This is the generic seam tokenguard's mid-stream budget breaker rides. No network. See docs/core.md.
"""

from types import SimpleNamespace

import pytest
from cendor.core import add_stream_observer, bus, instrument, remove_stream_observer
from cendor.core.instrument import _stream_observers
from cendor.core.types import LLMCall


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()
    # Defensive: never leak a registered observer across tests.
    for fn in list(_stream_observers):
        remove_stream_observer(fn)


def _chunk(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


def _usage_chunk(prompt, completion):
    return SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    )


def _sync_openai(chunks):
    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _async_openai(chunks):
    class Completions:
        async def create(self, **kwargs):
            async def agen():
                for c in chunks:
                    yield c

            return agen()

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_zero_observer_stream_is_byte_identical(events):
    # With no observer registered, the streamed chunks pass through exactly as before (the fast
    # path).
    chunks = [_chunk("Hel"), _chunk("lo"), _usage_chunk(3, 2)]
    client = instrument(_sync_openai(chunks))
    stream = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    got = list(stream)
    assert got == chunks  # identical objects, in order
    assert len(events) == 1
    assert events[0].usage.output_tokens == 2  # real usage, not the observer path


def test_observer_sees_each_delta_in_order(events):
    seen: list[tuple[str, str]] = []
    add_stream_observer(lambda call, text, thinking: seen.append((text, thinking)))
    chunks = [_chunk("Hel"), _chunk("lo"), _usage_chunk(3, 2)]
    client = instrument(_sync_openai(chunks))
    got = list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))

    assert got == chunks  # passthrough unchanged even with an (inert) observer armed
    # One observe call per chunk; the visible text deltas arrive in order; usage chunk has no text.
    assert [t for t, _ in seen] == ["Hel", "lo", ""]
    assert all(th == "" for _, th in seen)  # no thinking on a plain OpenAI stream


def test_observer_receives_the_live_llmcall(events):
    captured: list = []
    add_stream_observer(lambda call, text, thinking: captured.append(call))
    client = instrument(_sync_openai([_chunk("x"), _usage_chunk(1, 1)]))
    list(client.chat.completions.create(model="gpt-4o", messages=[], stream=True))
    assert captured  # observer ran
    assert all(isinstance(c, LLMCall) for c in captured)
    assert captured[0] is captured[-1]  # the SAME call object every chunk


def test_raise_aborts_stream_and_finalizes_once(events):
    # An observer that raises on the 2nd chunk must: stop iteration with that exception, close the
    # underlying stream, and emit exactly one LLMCall flagged estimated (partial settle).
    calls = {"n": 0}
    closed = {"v": False}

    def observer(call, text, thinking):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("cut")

    add_stream_observer(observer)

    class Stream:
        def __init__(self):
            self._it = iter([_chunk("a"), _chunk("b"), _chunk("c"), _usage_chunk(9, 9)])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._it)

        def close(self):
            closed["v"] = True

    class Completions:
        def create(self, **kwargs):
            return Stream()

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    stream = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    got = []
    with pytest.raises(RuntimeError, match="cut"):
        for ch in stream:
            got.append(ch)

    assert len(got) == 1  # only the 1st chunk reached the consumer; the crossing (2nd) is withheld
    assert closed["v"] is True  # underlying provider stream was closed on abort
    assert len(events) == 1  # exactly one finalize/emit
    assert events[0].metadata["streamed"] is True
    assert (
        events[0].metadata.get("usage_estimated") is True
    )  # partial estimate, no real usage chunk


async def test_async_raise_aborts_stream_and_finalizes_once(events):
    calls = {"n": 0}

    def observer(call, text, thinking):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("cut")

    add_stream_observer(observer)
    client = instrument(_async_openai([_chunk("a"), _chunk("b"), _chunk("c"), _usage_chunk(9, 9)]))
    stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)

    got = []
    with pytest.raises(RuntimeError, match="cut"):
        async for ch in stream:
            got.append(ch)

    assert len(got) == 1
    assert len(events) == 1
    assert events[0].metadata.get("usage_estimated") is True


def test_registration_is_idempotent(events):
    def observer(call, text, thinking):
        pass

    add_stream_observer(observer)
    add_stream_observer(observer)  # second add is a no-op
    assert _stream_observers.count(observer) == 1
    remove_stream_observer(observer)
    assert observer not in _stream_observers
    remove_stream_observer(observer)  # removing an absent observer is a no-op (no error)


def test_thinking_text_folded_into_estimate(events):
    # Anthropic thinking_delta streams visible reasoning text; the offline estimate must count it as
    # output and surface it as reasoning (no usage chunk -> estimated).
    chunks = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="thinking_delta", thinking="let me think about this carefully"
            ),
        ),
        SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="answer")
        ),
    ]

    class Messages:
        def create(self, **kwargs):
            return iter(chunks)

    client = instrument(SimpleNamespace(messages=Messages()))
    list(
        client.messages.create(
            model="claude-opus-4-8", messages=[{"role": "user", "content": "hi"}], stream=True
        )
    )

    call = events[0]
    assert call.metadata["usage_estimated"] is True
    assert call.usage.reasoning_tokens > 0  # visible thinking counted as reasoning
    assert (
        call.usage.output_tokens > call.usage.reasoning_tokens
    )  # visible answer text also counted
