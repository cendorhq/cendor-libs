"""Anthropic's ``messages.stream()`` / ``messages.parse()`` helpers — capture in **Python**.

Both were silent bypasses. Measured against the published shelf (`cendor-core` 1.16.0,
`anthropic` 0.120.2) with a mocked transport: `messages.stream()` emitted **zero** ``LLMCall``s
through every one of its three consumption paths (iteration, ``.text_stream``,
``.get_final_message()``) and `messages.parse()` emitted zero too — while the POST plainly happened.
Evidence: `plan/evidence-gapclose-2026-07-31/s1_probe_anthropic_stream_py.py`.

**Root cause, and why it is Python-only.** `Messages.stream` does not delegate to `create`; it
builds its own ``partial(self._post, "/v1/messages", …, stream=True)`` and hands that to a
``MessageStreamManager``. `Messages.parse` likewise POSTs its own request. So the wrapped `create`
had nothing to observe. In **TypeScript** the very same helpers are built *on* `create`
(``create({…, stream:true}).withResponse()``), so `@cendor/core` already captures them and adding
targets there would double-count — the same asymmetry `openai`'s `parse` has, for the same
codegen reason. That asymmetry is asserted from the TS side, not here.

The fakes below mirror the real SDK's shape exactly where it matters: a manager that issues the
request in ``__enter__`` and builds a helper object whose every consumption path funnels through
``_raw_stream`` (which is what core substitutes). Chunks arrive with a real cadence so a per-chunk
observer is distinguishable from a post-hoc one.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from cendor.core import (
    MISS,
    Reroute,
    add_interceptor,
    add_stream_observer,
    bus,
    instrument,
    remove_interceptor,
    remove_stream_observer,
)
from cendor.core.types import LLMCall

CHUNK_GAP_S = 0.02


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def calls(events) -> list[LLMCall]:
    return [e for e in events if isinstance(e, LLMCall)]


# --------------------------------------------------------------------------- the fake SDK


def _sse_chunks(input_tokens=11, output_tokens=7):
    """The Anthropic streamed event sequence, in the shape core's extractors read."""
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=1)
            ),
        ),
        SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="he")
        ),
        SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="llo")
        ),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=output_tokens)),
        SimpleNamespace(type="message_stop"),
    ]


class _RawStream:
    """The SDK's raw SSE stream — the object core substitutes. Sleeps between chunks."""

    def __init__(self, chunks, gap=CHUNK_GAP_S):
        self._chunks = list(chunks)
        self.closed = False
        self._gap = gap

    def __iter__(self):
        for i, ch in enumerate(self._chunks):
            if i:
                time.sleep(self._gap)
            yield ch

    def close(self):
        self.closed = True

    @property
    def response(self):
        return SimpleNamespace(headers={"request-id": "req_1"})


class _ARawStream(_RawStream):
    async def __aiter__(self):
        for i, ch in enumerate(self._chunks):
            if i:
                await asyncio.sleep(self._gap)
            yield ch

    async def close(self):
        self.closed = True


class _MessageStream:
    """Mirrors ``anthropic.lib.streaming.MessageStream``: three consumption paths, all of which
    resolve to ``self._raw_stream`` through *lazily started* generators."""

    def __init__(self, raw):
        self._raw_stream = raw
        self.text_stream = self.__stream_text__()
        self._iterator = self.__stream__()

    def __iter__(self):
        yield from self._iterator

    def __stream__(self):
        # Lazily reads `self._raw_stream` on first next() — the property core relies on.
        yield from self._raw_stream

    def __stream_text__(self):
        for ev in self:
            if getattr(ev, "type", "") == "content_block_delta":
                yield ev.delta.text

    def get_final_message(self):
        for _ in self:  # until_done()
            pass
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=11, output_tokens=7))

    def close(self):
        self._raw_stream.close()


class _AMessageStream(_MessageStream):
    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        async for ev in self._raw_stream:
            yield ev

    async def close(self):
        await self._raw_stream.close()


class _Manager:
    """``MessageStreamManager``: the request is issued by ``__enter__``, not by ``stream()``."""

    def __init__(self, issue):
        self._issue = issue
        self.entered = False
        self.stream = None

    def __enter__(self):
        self.entered = True
        self.stream = _MessageStream(self._issue())
        return self.stream

    def __exit__(self, *exc):
        if self.stream is not None:
            self.stream.close()
        return False


class _AManager(_Manager):
    async def __aenter__(self):
        self.entered = True
        self.stream = _AMessageStream(self._issue())
        return self.stream

    async def __aexit__(self, *exc):
        if self.stream is not None:
            await self.stream.close()
        return False


class _Messages:
    """An `anthropic`-shaped ``client.messages`` namespace."""

    def __init__(self, *, aio=False, stream_rides_create=False, chunks=None):
        self.posts: list[dict] = []
        self._aio = aio
        self._stream_rides_create = stream_rides_create
        self._chunks = chunks if chunks is not None else _sse_chunks()
        self.raw_streams: list[_RawStream] = []

    def create(self, **kwargs):
        self.posts.append(kwargs)
        if kwargs.get("stream"):
            raw = (_ARawStream if self._aio else _RawStream)(self._chunks)
            self.raw_streams.append(raw)
            return raw
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            content=[SimpleNamespace(type="text", text="hello")],
        )

    def stream(self, **kwargs):
        def issue():
            if self._stream_rides_create:
                # The TypeScript shape: the helper consumes the (wrapped) create.
                return self.create(**{**kwargs, "stream": True})
            self.posts.append({**kwargs, "stream": True})  # its own POST — today's Python shape
            raw = (_ARawStream if self._aio else _RawStream)(self._chunks)
            self.raw_streams.append(raw)
            return raw

        return (_AManager if self._aio else _Manager)(issue)

    def parse(self, **kwargs):
        self.posts.append({**kwargs, "parse": True})  # its own POST, like the real SDK
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            content=[SimpleNamespace(type="text", text='{"a":1}')],
        )


class _MessagesCreateOnly(_Messages):
    """An `anthropic` old enough to have neither helper — the `callable()` gates must skip both."""

    stream = None  # type: ignore[assignment]
    parse = None  # type: ignore[assignment]


def client(*, no_helpers=False, **kw):
    cls = _MessagesCreateOnly if no_helpers else _Messages
    return SimpleNamespace(messages=cls(**kw))


ARGS = dict(model="claude-haiku-4-5", max_tokens=16, messages=[{"role": "user", "content": "hi"}])


# --------------------------------------------------------------------------- capture


def test_stream_helper_emits_exactly_one_call_on_iteration(events):
    c = instrument(client())
    with c.messages.stream(**ARGS) as s:
        chunks = list(s)
    assert len(chunks) == 5  # chunks pass through unchanged
    got = calls(events)
    assert len(got) == 1
    assert got[0].provider == "anthropic"  # the internal tag never leaks
    assert got[0].model == "claude-haiku-4-5"
    assert got[0].usage.input_tokens == 11
    assert got[0].usage.output_tokens == 7
    assert "usage_estimated" not in got[0].metadata  # provider-reported, not estimated
    assert got[0].cost is not None


def test_stream_helper_counts_the_text_stream_path(events):
    """`.text_stream` never touches the helper's ``__iter__`` from outside — it is counted because
    core substitutes the RAW stream, which every path funnels through."""
    c = instrument(client())
    with c.messages.stream(**ARGS) as s:
        text = "".join(s.text_stream)
    assert text == "hello"
    got = calls(events)
    assert len(got) == 1
    assert got[0].usage.output_tokens == 7


def test_stream_helper_counts_the_get_final_message_path(events):
    c = instrument(client())
    with c.messages.stream(**ARGS) as s:
        final = s.get_final_message()
    assert final.usage.output_tokens == 7
    assert len(calls(events)) == 1


def test_stream_helper_returns_the_genuine_sdk_object(events):
    """The caller must get the SDK's own helper back — not a core wrapper that has to forward every
    method — so `.text_stream`/`.get_final_message()`/callbacks keep working verbatim."""
    c = instrument(client())
    with c.messages.stream(**ARGS) as s:
        assert isinstance(s, _MessageStream)
        list(s)


def test_parse_helper_is_captured(events):
    c = instrument(client())
    c.messages.parse(**ARGS)
    got = calls(events)
    assert len(got) == 1
    assert got[0].usage.input_tokens == 11


@pytest.mark.asyncio
async def test_async_stream_helper_is_captured(events):
    c = instrument(client(aio=True))
    async with c.messages.stream(**ARGS) as s:
        seen = [ch async for ch in s]
    assert len(seen) == 5
    got = calls(events)
    assert len(got) == 1
    assert got[0].usage.output_tokens == 7


# --------------------------------------------------------------------------- pre-flight governance


def test_stream_helper_is_blocked_before_any_request(events):
    """A budget's block must stop the call BEFORE the wire — the whole point of pre-flight."""

    class Blocked(Exception):
        pass

    def blocker(call):
        if isinstance(call, LLMCall):
            raise Blocked
        return MISS

    c = instrument(client())
    add_interceptor(blocker)
    try:
        with pytest.raises(Blocked):
            with c.messages.stream(**ARGS) as s:
                list(s)
    finally:
        remove_interceptor(blocker)
    assert c.messages.posts == []  # NEGATIVE CONTROL: nothing was sent
    assert calls(events) == []


def test_stream_helper_reroute_reaches_the_wire(events):
    """`guard()`'s redact-before-send has to rewrite what the provider actually receives."""

    def redactor(call):
        if isinstance(call, LLMCall):
            return Reroute(messages=[{"role": "user", "content": "[REDACTED]"}])
        return MISS

    c = instrument(client())
    add_interceptor(redactor)
    try:
        with c.messages.stream(**ARGS) as s:
            list(s)
    finally:
        remove_interceptor(redactor)
    assert c.messages.posts[0]["messages"] == [{"role": "user", "content": "[REDACTED]"}]
    got = calls(events)
    assert len(got) == 1
    assert got[0].metadata.get("rerouted") is True


def test_stream_helper_runs_stream_observers(events):
    """tokenguard's mid-stream breaker rides this seam; it must see the helper's chunks too."""
    seen: list[str] = []

    def observer(call, text, thinking):
        seen.append(text)

    c = instrument(client())
    add_stream_observer(observer)
    try:
        with c.messages.stream(**ARGS) as s:
            list(s)
    finally:
        remove_stream_observer(observer)
    assert "".join(seen) == "hello"


def test_stream_observer_raising_cuts_the_helper_stream(events):
    """Interceptor discipline: a raising observer closes the provider stream and settles once."""

    class Cut(Exception):
        pass

    def observer(call, text, thinking):
        if text:
            raise Cut

    c = instrument(client())
    add_stream_observer(observer)
    try:
        with pytest.raises(Cut):
            with c.messages.stream(**ARGS) as s:
                list(s)
    finally:
        remove_stream_observer(observer)
    assert c.messages.raw_streams[0].closed is True
    assert len(calls(events)) == 1


# --------------------------------------------------------------------------- negative controls


def test_no_double_count_when_the_helper_rides_create(events):
    """NEGATIVE CONTROL for the one way this fix could go wrong.

    Python's `messages.stream` POSTs its own request today (measured), so `create` is not involved.
    But TypeScript's is built on `create`, and a Python SDK adopting that shape would otherwise emit
    two ``LLMCall``s — and charge two budgets — for one HTTP request. The reentrancy guard makes the
    enclosing stream-manager wrapper the single accountant either way.
    """
    c = instrument(client(stream_rides_create=True))
    with c.messages.stream(**ARGS) as s:
        list(s)
    got = calls(events)
    assert len(got) == 1, f"double-counted: {len(got)} events for one request"
    assert got[0].usage.output_tokens == 7


def test_plain_create_is_untouched_by_the_guard(events):
    """The guard must not suppress an ordinary `create`: it is scoped to the manager's window."""
    c = instrument(client())
    c.messages.create(**ARGS)
    assert len(calls(events)) == 1
    stream = c.messages.create(**ARGS, stream=True)
    list(stream)
    assert len(calls(events)) == 2


def test_uninstrumented_client_emits_nothing(events):
    c = client()
    with c.messages.stream(**ARGS) as s:
        list(s)
    c.messages.parse(**ARGS)
    assert calls(events) == []


def test_never_entering_the_manager_emits_nothing(events):
    """No entry means no request means no event. A phantom call here is worse than the gap was."""
    c = instrument(client())
    manager = c.messages.stream(**ARGS)
    assert c.messages.posts == []
    assert calls(events) == []
    del manager


def test_older_sdk_without_the_helpers_still_wraps_create(events):
    """`callable()`-gated detection: an SDK with neither helper is wrapped exactly as before."""
    c = instrument(client(no_helpers=True))
    assert not callable(c.messages.stream)  # present but not callable ⇒ not a target
    assert not callable(c.messages.parse)
    c.messages.create(**ARGS)
    assert len(calls(events)) == 1


def test_instrument_is_idempotent_over_the_new_targets(events):
    """Re-wrapping must not stack proxies (one event, not two)."""
    c = client()
    instrument(c)
    instrument(c)
    with c.messages.stream(**ARGS) as s:
        list(s)
    assert len(calls(events)) == 1


def test_estimate_is_flagged_when_the_provider_reports_no_usage(events):
    """Honest claims: no usage in the stream ⇒ an offline estimate, explicitly flagged."""
    chunks = [
        SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hello")
        ),
        SimpleNamespace(type="message_stop"),
    ]
    c = instrument(client(chunks=chunks))
    with c.messages.stream(**ARGS) as s:
        list(s)
    got = calls(events)
    assert len(got) == 1
    assert got[0].metadata.get("usage_estimated") is True
