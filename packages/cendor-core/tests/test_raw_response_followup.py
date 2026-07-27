"""The raw-response family, part two — the edges the 1.14.1 wave left open.

Red-first. Each case was measured against the **real** openai SDK first (2026-07-27 follow-up
evidence pack, `plan/evidence-ripple-followup-2026-07-27/`); the mocks here reproduce the shapes
that measurement found, with no network and no SDK import.

* **B1** — resolving ``client.chat.completions.with_raw_response`` *before* ``instrument()``
  snapshots the un-wrapped method, so the call emitted **zero** events, silently. openai builds
  those accessors as ``cached_property``, so a resolved one is visible in the instance ``__dict__``
  and can be evicted — measured: eviction restores capture. A reference the caller already *held*
  cannot be reached, and stays an honest limit.
* **C1** — ``chat.completions.parse`` was not an instrumented entrypoint. ``langchain-openai``
  1.4.1 takes it on **every** ``with_structured_output()`` call over Chat Completions
  (``chat_models/base.py:1719``), so that whole branch was invisible to budgets, guards and audit.
  Like ``responses.parse`` it issues its own request (``openai/resources/chat/completions``
  ``parse`` → ``self._post``), so wrapping it cannot double-count.
* **C1-b** — a raw-response call with ``stream=True`` (``langchain-openai`` ``base.py:1498``) hands
  back an *envelope*, not a stream. Core wrapped it as a stream anyway: iterating raised
  ``AttributeError: 'LegacyAPIResponse' object has no attribute '__aiter__'`` from inside cendor,
  and the working path (``env.parse()``) bypassed the proxy entirely ⇒ no capture.
* **B3** — the decoded envelope body is now published on the call for recorders, so ``cassette``
  no longer has to walk the envelope's object graph (which recursed to the stack limit).
"""

import asyncio
import functools
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.types import Usage

CHAT_BODY = {
    "id": "chatcmpl-1",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"word":"ok"}'}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12},
}


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


class Envelope:
    """openai's raw-response envelope, duck-typed: headers + a buffered body, **no** ``usage``."""

    def __init__(self, body: dict) -> None:
        self.headers = {"x-request-id": "req_1"}
        self.status_code = 200
        self._body = body
        self.http_response = SimpleNamespace(json=lambda: body)

    def parse(self) -> object:
        return SimpleNamespace(**self._body)


class StreamEnvelope:
    """A raw-response envelope for a **streamed** call: the body was never read (SSE), and the
    caller reaches the stream through ``parse()``. Measured shape — `LegacyAPIResponse`, no
    ``__aiter__``, ``parse()`` → ``AsyncStream``."""

    def __init__(self, chunks: list) -> None:
        self.headers = {"x-request-id": "req_1"}
        self._chunks = chunks
        self.parses = 0

        def _json() -> dict:
            raise RuntimeError("ResponseNotRead: the response body has not been read")

        self.http_response = SimpleNamespace(json=_json)

    def parse(self) -> object:
        self.parses += 1

        async def agen():
            for chunk in self._chunks:
                yield chunk

        return agen()


def chunk(text: str | None = None, usage: dict | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(content=text)
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=None if text else "stop")
    return SimpleNamespace(
        id="chatcmpl-1",
        model="gpt-4o-mini",
        choices=[choice],
        usage=SimpleNamespace(**usage) if usage else None,
    )


STREAM_CHUNKS = [
    chunk("hello "),
    chunk("world"),
    chunk(None, {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}),
]


class Completions:
    """A duck-typed ``chat.completions`` namespace with the two accessors openai exposes as
    ``cached_property`` — so the eviction under test has something real to evict."""

    def __init__(self, returns: object) -> None:
        self._returns = returns
        self.created = 0
        self.parsed = 0

    async def create(self, **kwargs: object) -> object:
        self.created += 1
        return self._returns

    async def parse(self, **kwargs: object) -> object:
        # openai's `parse` issues its OWN request (`self._post`) rather than delegating to
        # `create` — so instrumenting both cannot double-count. Modelled here.
        self.parsed += 1
        return self._returns

    @functools.cached_property
    def with_raw_response(self) -> object:
        return SimpleNamespace(create=self.create, parse=self.parse)

    @functools.cached_property
    def with_streaming_response(self) -> object:
        return SimpleNamespace(create=self.create)


def chat_client(returns: object) -> tuple[object, Completions]:
    comp = Completions(returns)
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return client, comp


# --- C1: chat.completions.parse is an instrumented entrypoint --------------------------------


def test_chat_completions_parse_is_captured(events):
    client, comp = chat_client(SimpleNamespace(**CHAT_BODY))
    instrument(client)
    asyncio.run(client.chat.completions.parse(model="gpt-4o-mini", messages=[]))

    assert len(events) == 1, "langchain's with_structured_output branch must reach the bus"
    assert events[0].provider == "openai" and events[0].model == "gpt-4o-mini"


def test_chat_completions_parse_does_not_double_count(events):
    client, comp = chat_client(Envelope(CHAT_BODY))
    instrument(client)
    asyncio.run(client.chat.completions.parse(model="gpt-4o-mini", messages=[]))

    assert comp.parsed == 1 and comp.created == 0  # parse issues its own request
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=11, output_tokens=1)
    assert events[0].cost is not None and events[0].cost.amount > 0


def test_a_client_without_parse_is_still_instrumented(events):
    # `callable()`-gated, exactly like responses.parse: an older SDK simply has no `parse`.
    comp = SimpleNamespace(create=Completions(SimpleNamespace(**CHAT_BODY)).create)
    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    asyncio.run(client.chat.completions.create(model="gpt-4o-mini", messages=[]))

    assert len(events) == 1


# --- B1: a cached raw-response accessor resolved before instrument() -------------------------


def test_with_raw_response_resolved_before_instrument_still_captures(events):
    client, comp = chat_client(Envelope(CHAT_BODY))
    _ = comp.with_raw_response  # the app reaches in first (langchain / MAF construction order)
    instrument(client)

    asyncio.run(client.chat.completions.with_raw_response.create(model="gpt-4o-mini", messages=[]))

    assert len(events) == 1, "the stale cached_property must not shadow the wrapped method"
    assert events[0].usage == Usage(input_tokens=11, output_tokens=1)
    assert events[0].metadata["raw_response_envelope"] is True


def test_with_streaming_response_is_evicted_too(events):
    client, comp = chat_client(Envelope(CHAT_BODY))
    _ = comp.with_streaming_response
    instrument(client)

    asyncio.run(
        client.chat.completions.with_streaming_response.create(model="gpt-4o-mini", messages=[])
    )

    assert len(events) == 1


def test_an_already_held_accessor_reference_stays_uninstrumented(events):
    """The honest limit: eviction fixes the next *access*, not a reference the caller kept."""
    client, comp = chat_client(Envelope(CHAT_BODY))
    held = comp.with_raw_response  # captured into a local before instrument()
    instrument(client)

    asyncio.run(held.create(model="gpt-4o-mini", messages=[]))

    assert events == []


def test_eviction_does_not_disturb_a_client_without_those_accessors(events):
    plain = SimpleNamespace(usage=SimpleNamespace(input_tokens=14, output_tokens=2))

    async def create(**kwargs):
        return plain

    client = instrument(SimpleNamespace(responses=SimpleNamespace(create=create)))
    asyncio.run(client.responses.create(model="gpt-4o-mini", input="hi"))

    assert len(events) == 1 and events[0].usage == Usage(input_tokens=14, output_tokens=2)


# --- C1-b: a raw-response envelope handed back for a STREAMED call ---------------------------


def test_a_streamed_raw_response_returns_the_envelope_and_captures_on_parse(events):
    env = StreamEnvelope(STREAM_CHUNKS)
    client, _comp = chat_client(env)
    instrument(client)

    async def drive():
        returned = await client.chat.completions.with_raw_response.create(
            model="gpt-4o-mini", messages=[], stream=True
        )
        # the caller's contract: the SDK returns an ENVELOPE, and the stream is behind parse()
        assert returned is env, "core must not hand back a stream proxy in place of the envelope"
        text = ""
        async for c in returned.parse():
            text += (c.choices[0].delta.content or "") if c.choices else ""
        return text

    assert asyncio.run(drive()) == "hello world"
    assert len(events) == 1, "consuming the stream behind parse() must still emit one LLMCall"
    assert events[0].usage == Usage(input_tokens=9, output_tokens=2)


def test_parsing_a_streamed_envelope_twice_emits_one_call(events):
    env = StreamEnvelope(STREAM_CHUNKS)
    client, _comp = chat_client(env)
    instrument(client)

    async def drive():
        returned = await client.chat.completions.with_raw_response.create(
            model="gpt-4o-mini", messages=[], stream=True
        )
        first = returned.parse()
        second = returned.parse()
        assert first is second, "parse() must be memoized so the call is accounted once"
        async for _ in first:
            pass

    asyncio.run(drive())
    assert len(events) == 1


def test_a_normal_streamed_call_still_returns_the_stream_proxy(events):
    async def create(**kwargs):
        async def agen():
            for c in STREAM_CHUNKS:
                yield c

        return agen()

    client = instrument(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )

    async def drive():
        stream = await client.chat.completions.create(model="gpt-4o-mini", messages=[], stream=True)
        assert hasattr(stream, "__aiter__"), "an ordinary stream must keep the proxy"
        async for _ in stream:
            pass

    asyncio.run(drive())
    assert len(events) == 1 and events[0].usage == Usage(input_tokens=9, output_tokens=2)


# --- B3: publish the decoded body for recorders ----------------------------------------------


def test_metadata_carries_the_decoded_envelope_body_for_recorders(events):
    client, _comp = chat_client(Envelope(CHAT_BODY))
    instrument(client)
    asyncio.run(client.chat.completions.create(model="gpt-4o-mini", messages=[]))

    assert events[0].metadata["response_body"] == CHAT_BODY
    # the caller-facing value is untouched — recorders get the payload, everyone else the envelope
    assert isinstance(events[0].metadata["response"], Envelope)


def test_a_normal_response_publishes_no_response_body(events):
    plain = SimpleNamespace(usage=SimpleNamespace(input_tokens=14, output_tokens=2))

    async def create(**kwargs):
        return plain

    client = instrument(SimpleNamespace(responses=SimpleNamespace(create=create)))
    asyncio.run(client.responses.create(model="gpt-4o-mini", input="hi"))

    assert "response_body" not in events[0].metadata
