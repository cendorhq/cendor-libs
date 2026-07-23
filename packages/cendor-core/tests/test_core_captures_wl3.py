"""W-L3 core captures: L5 async-detect repair, L3 Bedrock converse_stream, L1 HF gated injection.

Red-first (GC-D10) for L5: a sync-looking method that returns an awaitable must still capture usage.
No network — mock clients only.
"""

from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.instrument import _accepts_stream_options
from cendor.core.types import LLMCall, Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


# --- L5: async-detect repair (misdetected async client returns an awaitable) ----------------


def test_l5_sync_looking_method_returning_awaitable_captures_usage(events):
    # `create` is a plain (non-`async def`) function that returns a coroutine —
    # iscoroutinefunction()
    # is False, so core picks the sync wrapper. Without the awaitable-continuation repair, usage is
    # silently lost on the un-awaited coroutine. This is the L5 trap.
    async def _real():
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))

    class Completions:
        def create(self, **kwargs):  # NOT async def -> misdetected
            return _real()

    import asyncio

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    async def _drive():
        # The sync wrapper returns the awaitable continuation; awaiting it drives the repair.
        return await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    resp = asyncio.run(_drive())
    assert resp is not None
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=100, output_tokens=50)


async def test_l5_awaitable_streaming_captures_usage(events):
    # Misdetected async client + stream=True: the awaited value is an async iterator; the repair
    # must route it through the async stream proxy so usage still emits once.
    async def _chunkgen():
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None
        )
        yield SimpleNamespace(
            choices=[], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        )

    async def _real():
        return _chunkgen()

    class Completions:
        def create(self, **kwargs):  # NOT async def
            return _real()

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    stream = await client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    got = [c async for c in stream]
    assert len(got) == 2
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=10, output_tokens=5)


# --- L3: Bedrock converse_stream (always-stream, member shape) -------------------------------


def test_l3_bedrock_converse_stream_captures_usage(events):
    # boto3 converse_stream: no stream= kwarg; the EventStream is the "stream" member of a dict
    # response. The metadata event carries usage.
    stream_events = [
        {"contentBlockDelta": {"delta": {"text": "hel"}}},
        {"contentBlockDelta": {"delta": {"text": "lo"}}},
        {"metadata": {"usage": {"inputTokens": 40, "outputTokens": 12}}},
    ]

    class Client:
        def converse(self, **kwargs):  # present so detection keys off Bedrock
            return {}

        def converse_stream(self, **kwargs):
            return {"stream": iter(stream_events), "ResponseMetadata": {"HTTPStatusCode": 200}}

    client = instrument(Client())
    response = client.converse_stream(
        modelId="claude-sonnet-4-6", messages=[{"role": "user", "content": [{"text": "hi"}]}]
    )
    assert "ResponseMetadata" in response  # dict shape preserved
    got = list(response["stream"])
    assert got == stream_events  # chunks pass through unchanged
    assert len(events) == 1
    call = events[0]
    assert isinstance(call, LLMCall)
    assert call.provider == "bedrock"  # public provider, not the internal bedrock_stream tag
    assert call.model == "claude-sonnet-4-6"
    assert call.usage == Usage(input_tokens=40, output_tokens=12)
    assert not call.metadata.get("usage_estimated")  # real usage from the metadata event


def test_l3_bedrock_converse_stream_thinking_estimate(events):
    # reasoningContent deltas count as visible thinking in the offline estimate (no metadata event).
    stream_events = [
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking hard about it"}}}},
        {"contentBlockDelta": {"delta": {"text": "final answer"}}},
    ]

    class Client:
        def converse(self, **kwargs):
            return {}

        def converse_stream(self, **kwargs):
            return {"stream": iter(stream_events)}

    client = instrument(Client())
    list(
        client.converse_stream(
            modelId="claude-sonnet-4-6", messages=[{"role": "user", "content": [{"text": "hi"}]}]
        )["stream"]
    )
    call = events[0]
    assert call.metadata["usage_estimated"] is True
    assert call.usage.reasoning_tokens > 0


# --- L1: HF stream_options injection is signature-gated -------------------------------------


def test_l1_accepts_stream_options_gate():
    def with_opt(self, *, messages, stream=False, stream_options=None):  # newer hub
        pass

    def without_opt(self, *, messages, stream=False):  # older hub
        pass

    def only_kwargs(self, **kwargs):  # a **kwargs catch-all must NOT count (4xx risk)
        pass

    assert _accepts_stream_options(with_opt) is True
    assert _accepts_stream_options(without_opt) is False
    assert _accepts_stream_options(only_kwargs) is False


def test_l1_hf_injects_include_usage_only_when_signature_supports_it(events):
    injected = {}

    class HFNewer:
        def chat_completion(self, *, model, messages, stream=False, stream_options=None):
            injected["opts"] = stream_options
            return iter(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None
                    )
                ]
            )

    client = instrument(HFNewer())
    list(
        client.chat_completion(
            model="meta-llama/x", messages=[{"role": "user", "content": "hi"}], stream=True
        )
    )
    assert injected["opts"] == {"include_usage": True}  # injected for a signature that accepts it

    bus._reset()
    injected.clear()
    seen2: list = []
    bus.subscribe(seen2.append)

    class HFOlder:
        def chat_completion(self, *, model, messages, stream=False):  # no stream_options param
            injected["called"] = True
            return iter(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None
                    )
                ]
            )

    client2 = instrument(HFOlder())
    # If injection were blind, this call would raise TypeError (unexpected kwarg).
    list(
        client2.chat_completion(
            model="meta-llama/x", messages=[{"role": "user", "content": "hi"}], stream=True
        )
    )
    assert injected["called"] is True  # no crash — not injected
