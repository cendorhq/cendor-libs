"""Gemini streaming capture — ``google-genai``'s ``generate_content_stream`` (sync + aio).

The google-genai SDK streams through a **separate method**, not a ``stream=True`` kwarg, so before
core 1.15 a streamed Gemini call emitted **nothing at all** (measured live 2026-07-31: zero
``LLMCall``s in both languages). These pin the fix: one ``LLMCall`` on completion, real usage off
the final chunk's ``usage_metadata``, an offline estimate flagged when the provider reports none,
chunks passed through unchanged, and the stream-observer seam (tokenguard's breaker) firing.

No network. Chunks arrive with a **real cadence** — a stream that yields everything instantly can't
tell a per-chunk observer apart from a post-hoc one (org rail), so ``_CadencedStream`` sleeps
between chunks and records the wall-clock gap the test asserts on.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from cendor.core import add_stream_observer, bus, instrument, remove_stream_observer
from cendor.core.types import Usage

CHUNK_GAP_S = 0.02  # per-chunk delay: small enough to keep tests fast, large enough to measure


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def _chunk(text, prompt=None, candidates=None, thoughts=None):
    """One google-genai stream chunk. Gemini puts ``usage_metadata`` on EVERY chunk holding the
    *running* totals (measured), so a chunk with counts is normal, not terminal."""
    usage = None
    if prompt is not None:
        usage = SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=candidates or 0,
            thoughts_token_count=thoughts,
        )
    return SimpleNamespace(text=text, usage_metadata=usage)


class _CadencedStream:
    """Sync iterator that sleeps between chunks (real cadence) and records ``close()``."""

    def __init__(self, chunks, gap=CHUNK_GAP_S):
        self._chunks = list(chunks)
        self._i = 0
        self._gap = gap
        self.closed = False
        self.yielded_at: list[float] = []

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= len(self._chunks):
            raise StopIteration
        if self._i:
            time.sleep(self._gap)
        self.yielded_at.append(time.perf_counter())
        ch = self._chunks[self._i]
        self._i += 1
        return ch

    def close(self):
        self.closed = True


class _ACadencedStream:
    """Async twin of :class:`_CadencedStream`."""

    def __init__(self, chunks, gap=CHUNK_GAP_S):
        self._chunks = list(chunks)
        self._i = 0
        self._gap = gap
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        if self._i:
            await asyncio.sleep(self._gap)
        ch = self._chunks[self._i]
        self._i += 1
        return ch

    async def aclose(self):
        self.closed = True


def _sync_client(stream):
    """A google-genai-shaped client: ``client.models.generate_content{,_stream}``."""

    class Models:
        def generate_content(self, **kwargs):
            return SimpleNamespace(usage_metadata=None)

        def generate_content_stream(self, **kwargs):
            return stream

    return SimpleNamespace(models=Models())


def _async_client(stream):
    """``client.aio.models.generate_content_stream`` is a coroutine returning an async iterator."""

    class AioModels:
        async def generate_content(self, **kwargs):
            return SimpleNamespace(usage_metadata=None)

        async def generate_content_stream(self, **kwargs):
            return stream

    return SimpleNamespace(aio=SimpleNamespace(models=AioModels()))


# --------------------------------------------------------------------------- detection + capture


def test_sync_stream_emits_one_llmcall_with_real_usage(events):
    stream = _CadencedStream([_chunk("One, ", 4, 3), _chunk("two, three.", 4, 7)])
    client = instrument(_sync_client(stream))

    out = [
        c.text
        for c in client.models.generate_content_stream(
            model="gemini-2.5-flash", contents="Count to three"
        )
    ]

    assert out == ["One, ", "two, three."]  # chunks pass through unchanged
    assert len(events) == 1, "exactly one LLMCall, emitted on completion"
    call = events[0]
    assert call.provider == "google"  # not the internal "google_stream" tag
    assert call.model == "gemini-2.5-flash"
    assert call.metadata["streamed"] is True
    assert not call.metadata.get("usage_estimated"), "real usage was reported"
    # Gemini's per-chunk counts are cumulative -> the LAST chunk is the final total, not the first.
    assert call.usage == Usage(input_tokens=4, output_tokens=7)
    assert call.cost is not None and call.cost.amount > 0  # gemini-2.5-flash is in the snapshot
    # negative control on the cadence itself: the chunks really did arrive apart in time
    assert stream.yielded_at[-1] - stream.yielded_at[0] >= CHUNK_GAP_S


def test_cumulative_usage_takes_the_last_chunk_not_the_first(events):
    # The whole point of the google branch in _stream_usage. First chunk says 3 output tokens; the
    # generic "first usage-bearing chunk wins" rule would record 3 and under-count by 9.
    stream = _CadencedStream([_chunk("a", 5, 3), _chunk("b", 5, 8), _chunk("c", 5, 12)])
    client = instrument(_sync_client(stream))
    list(client.models.generate_content_stream(model="gemini-2.5-flash", contents="hi"))
    assert events[0].usage == Usage(input_tokens=5, output_tokens=12)


def test_thoughts_fold_into_output_and_surface_as_reasoning(events):
    stream = _CadencedStream([_chunk("x", 6, 10, thoughts=4)])
    client = instrument(_sync_client(stream))
    list(client.models.generate_content_stream(model="gemini-2.5-flash", contents="hi"))
    assert events[0].usage == Usage(input_tokens=6, output_tokens=14, reasoning_tokens=4)


def test_absent_usage_falls_back_to_a_flagged_estimate(events):
    # Rail: a stream with no usage must still emit, with cendor's estimate FLAGGED as an estimate.
    stream = _CadencedStream([_chunk("hello "), _chunk("world")])
    client = instrument(_sync_client(stream))
    list(client.models.generate_content_stream(model="gemini-2.5-flash", contents="hi there"))
    call = events[0]
    assert call.metadata["usage_estimated"] is True
    assert call.usage is not None and call.usage.output_tokens > 0


async def test_async_stream_emits_one_llmcall(events):
    stream = _ACadencedStream([_chunk("One, ", 4, 3), _chunk("two.", 4, 6)])
    client = instrument(_async_client(stream))

    out = []
    async for chunk in await client.aio.models.generate_content_stream(
        model="gemini-2.5-flash", contents="Count"
    ):
        out.append(chunk.text)

    assert out == ["One, ", "two."]
    assert len(events) == 1
    assert events[0].provider == "google"
    assert events[0].usage == Usage(input_tokens=4, output_tokens=6)
    assert events[0].metadata["streamed"] is True


def test_non_stream_twin_still_works_alongside(events):
    # Wrapping generate_content_stream must not disturb the plain generate_content target.
    stream = _CadencedStream([_chunk("x", 1, 1)])
    client = instrument(_sync_client(stream))
    client.models.generate_content(model="gemini-2.5-flash", contents="hi")
    assert len(events) == 1
    assert events[0].provider == "google"
    assert not events[0].metadata.get("streamed")


def test_instrument_is_idempotent_over_the_stream_target():
    stream = _CadencedStream([_chunk("x", 1, 1)])
    client = _sync_client(stream)
    instrument(client)
    first = client.models.generate_content_stream
    instrument(client)
    assert client.models.generate_content_stream is first, "no double-wrap"


def test_a_client_without_the_stream_method_is_untouched(events):
    # Negative control: an older google-genai with no generate_content_stream must still work.
    class Models:
        def generate_content(self, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=2, candidates_token_count=1)
            )

    client = instrument(SimpleNamespace(models=Models()))
    assert not hasattr(client.models, "generate_content_stream")
    client.models.generate_content(model="gemini-2.5-flash", contents="hi")
    assert len(events) == 1


# --------------------------------------------------------------------------- observer seam


def test_stream_observer_sees_each_chunk_and_raising_aborts(events):
    """The seam tokenguard's mid-stream breaker rides: per-chunk callback; raising cuts it."""
    seen: list[str] = []

    def observer(call, text, thinking):
        seen.append(text)
        if len(seen) == 2:
            raise RuntimeError("cut")

    stream = _CadencedStream([_chunk("a", 1, 1), _chunk("b", 1, 2), _chunk("c", 1, 3)])
    add_stream_observer(observer)
    try:
        client = instrument(_sync_client(stream))
        with pytest.raises(RuntimeError, match="cut"):
            list(client.models.generate_content_stream(model="gemini-2.5-flash", contents="hi"))
    finally:
        remove_stream_observer(observer)

    assert seen == ["a", "b"], "observer ran per chunk, and the third never arrived"
    assert stream.closed is True, "the underlying provider stream was closed on abort"
    assert len(events) == 1, "the partial call is still finalized exactly once"
    assert events[0].metadata["streamed"] is True
