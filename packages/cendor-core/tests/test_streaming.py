"""Streaming instrumentation: chunks pass through unchanged, usage/cost emitted once. No network."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.types import LLMCall, Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


# --- OpenAI-shaped streaming chunks ---------------------------------------------------------


def _chunk(text):
    """A content delta chunk (no usage), OpenAI streaming shape."""
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


def _usage_chunk(prompt, completion):
    """The final usage chunk emitted with stream_options={'include_usage': True}."""
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


def test_sync_stream_passthrough_and_real_usage(events):
    chunks = [_chunk("Hel"), _chunk("lo"), _usage_chunk(100, 50)]
    client = instrument(_sync_openai(chunks))
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )

    assert events == []  # lazy: nothing emitted until the stream is consumed
    got = list(stream)

    assert got == chunks  # every chunk passes through, in order, unchanged
    assert len(events) == 1  # exactly one LLMCall, at completion
    call = events[0]
    assert isinstance(call, LLMCall)
    assert call.metadata["streamed"] is True
    assert call.usage == Usage(input_tokens=100, output_tokens=50)
    assert call.cost.amount == Decimal("0.00075")  # 100*2.5e-6 + 50*1e-5
    assert not call.metadata.get("usage_estimated")
    assert call.latency_ms is not None and call.latency_ms >= 0


def test_sync_stream_estimates_usage_when_provider_omits_it(events):
    # No usage chunk (OpenAI without include_usage) -> offline estimate, flagged honestly.
    chunks = [_chunk("Hello world, "), _chunk("this is streamed output text.")]
    client = instrument(_sync_openai(chunks))
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "count my tokens please"}],
        stream=True,
    )
    list(stream)

    call = events[0]
    assert call.metadata["usage_estimated"] is True
    assert call.usage.input_tokens > 0  # counted from the request messages
    assert call.usage.output_tokens > 0  # counted from the accumulated streamed text


async def test_async_stream_passthrough_and_real_usage(events):
    chunks = [_chunk("Hi"), _usage_chunk(10, 5)]
    client = instrument(_async_openai(chunks))
    stream = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )

    got = [c async for c in stream]
    assert got == chunks
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=10, output_tokens=5)
    assert events[0].metadata["streamed"] is True


def test_anthropic_event_stream_usage(events):
    # Anthropic splits usage across message_start (input) and message_delta (output) events.
    chunks = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=80, cache_read_input_tokens=0)
            ),
        ),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="answer")),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=12)),
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
    assert call.provider == "anthropic"
    assert call.usage == Usage(input_tokens=80, output_tokens=12)
    assert not call.metadata.get("usage_estimated")  # real usage recovered from events


def test_anthropic_stream_folds_cache_reads_into_input(events):
    # Streaming must apply the same subset normalization as the non-stream path: Anthropic's
    # message_start input_tokens excludes cache reads, so fold cache_read_input_tokens back in.
    chunks = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=80, cache_read_input_tokens=20)
            ),
        ),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="answer")),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=12)),
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
    assert call.usage == Usage(input_tokens=100, output_tokens=12, cached_tokens=20)
    # (100-20)*input + 12*output + 20*cached = 0.000005*80 + 0.000025*12 + 0.0000005*20
    # = 0.0004 + 0.0003 + 0.00001 = 0.00071
    assert call.cost.amount == Decimal("0.00071")


def test_early_break_still_emits(events):
    chunks = [_chunk("a"), _chunk("b"), _chunk("c"), _usage_chunk(5, 5)]
    client = instrument(_sync_openai(chunks))
    stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    next(stream)  # consume one chunk, then abandon the stream
    stream.close()  # closing the generator runs its finally -> finalize/emit

    assert len(events) == 1  # a streamed call that's abandoned early is still accounted for
    assert events[0].metadata["streamed"] is True


# --- Bedrock / Gemini / Ollama streaming usage recovery -------------------------------------


def test_bedrock_stream_recovers_usage_from_metadata_event(events):
    # Bedrock streams usage on a `metadata` event (camelCase token keys).
    chunks = [
        SimpleNamespace(contentBlockDelta=SimpleNamespace(delta=SimpleNamespace(text="hi"))),
        SimpleNamespace(
            metadata=SimpleNamespace(usage=SimpleNamespace(inputTokens=30, outputTokens=12))
        ),
    ]

    class Client:
        def converse(self, **kwargs):
            return iter(chunks)

    client = instrument(Client())
    got = list(
        client.converse(
            modelId="claude-sonnet-4-6",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            stream=True,
        )
    )
    assert got == chunks
    call = events[0]
    assert call.provider == "bedrock"
    assert call.usage == Usage(input_tokens=30, output_tokens=12)
    assert not call.metadata.get("usage_estimated")  # real usage recovered from the metadata event


def test_gemini_stream_recovers_usage_from_final_chunk(events):
    # Gemini carries usage on a final full-response-shaped chunk (usage_metadata).
    chunks = [
        SimpleNamespace(text="par", usage_metadata=None),
        SimpleNamespace(
            text="tial",
            usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=20),
        ),
    ]

    class Models:
        def generate_content(self, **kwargs):
            return iter(chunks)

    client = instrument(SimpleNamespace(models=Models()))
    got = list(client.models.generate_content(model="gemini-1.5-pro", contents="hi", stream=True))
    assert got == chunks
    call = events[0]
    assert call.provider == "google"
    assert call.usage == Usage(input_tokens=40, output_tokens=20)
    assert not call.metadata.get("usage_estimated")


def test_ollama_stream_recovers_usage_from_final_chunk(events):
    # Ollama streams token counts top-level on the final chunk (prompt_eval_count / eval_count).
    chunks = [
        {"message": {"content": "par"}},
        {"message": {"content": "tial"}, "prompt_eval_count": 7, "eval_count": 5},
    ]

    class Client:
        def chat(self, **kwargs):
            return iter(chunks)

    client = instrument(Client())
    got = list(
        client.chat(model="llama3", messages=[{"role": "user", "content": "hi"}], stream=True)
    )
    assert got == chunks
    call = events[0]
    assert call.provider == "ollama"
    assert call.usage == Usage(input_tokens=7, output_tokens=5)
    assert not call.metadata.get("usage_estimated")
