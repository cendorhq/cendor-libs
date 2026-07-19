"""instrument() embeddings capture (1.6.0): mocked openai-shaped clients, no network.

`embeddings.create` on an openai-shaped client is wrapped like chat/responses: the pre-flight
interceptor pass runs (budget block / guard redaction apply), and the emitted `LLMCall` carries
`metadata["embedding"] = True`, usage from `response.usage`, and cost from the price table.
"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.instrument import MISS, Reroute, add_interceptor, remove_interceptor
from cendor.core.types import LLMCall


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def _embeddings_client(seen_kwargs, prompt_tokens=8):
    class Embeddings:
        def create(self, **kwargs):
            seen_kwargs.update(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2])],
                usage=SimpleNamespace(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens),
            )

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()), embeddings=Embeddings())


def _async_embeddings_client(prompt_tokens=8):
    class Embeddings:
        async def create(self, **kwargs):
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1])],
                usage=SimpleNamespace(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens),
            )

    return SimpleNamespace(embeddings=Embeddings())


def test_embeddings_create_emits_llmcall_with_golden_usage_and_cost(events):
    kw: dict = {}
    client = instrument(_embeddings_client(kw, prompt_tokens=1000))
    client.embeddings.create(model="text-embedding-3-small", input="hello world")

    calls = [e for e in events if isinstance(e, LLMCall)]
    assert len(calls) == 1
    call = calls[0]
    assert call.provider == "openai"  # internal openai_embeddings surfaces as openai
    assert call.model == "text-embedding-3-small"
    assert call.metadata["embedding"] is True
    assert call.usage.input_tokens == 1000 and call.usage.output_tokens == 0
    # golden: $0.02/1M -> 0.00000002/token * 1000 = 0.00002
    assert call.cost is not None and call.cost.amount == Decimal("0.00002")
    assert call.metadata["cost_estimated"] is True


def test_embeddings_input_normalized_to_messages(events):
    kw: dict = {}
    client = instrument(_embeddings_client(kw))
    client.embeddings.create(model="text-embedding-3-small", input=["a", "b"])
    call = [e for e in events if isinstance(e, LLMCall)][0]
    assert call.messages == [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]


def test_embeddings_async_create_captured(events):
    client = instrument(_async_embeddings_client(prompt_tokens=10))
    asyncio.run(client.embeddings.create(model="text-embedding-3-small", input="hi"))
    calls = [e for e in events if isinstance(e, LLMCall)]
    assert len(calls) == 1 and calls[0].usage.input_tokens == 10
    assert calls[0].metadata["embedding"] is True


def test_embeddings_preflight_interceptor_can_block(events):
    # The pre-flight pass runs before the provider call — a raising interceptor stops it.
    kw: dict = {}
    client = instrument(_embeddings_client(kw))

    class Nope(Exception):
        pass

    def block(call):
        if isinstance(call, LLMCall) and call.metadata.get("embedding"):
            raise Nope("blocked")
        return MISS

    add_interceptor(block)
    try:
        with pytest.raises(Nope):
            client.embeddings.create(model="text-embedding-3-small", input="hello")
    finally:
        remove_interceptor(block)
    assert kw == {}  # the provider was never called


def test_embeddings_reroute_rewrites_input_shape(events):
    # A Reroute(messages=...) (e.g. guard redact-before-send) maps back to the raw `input` shape.
    kw: dict = {}
    client = instrument(_embeddings_client(kw))

    def scrubber(call):
        if isinstance(call, LLMCall) and call.metadata.get("embedding"):
            cleaned = [{"role": "user", "content": "[email]"}]
            return Reroute(messages=cleaned)
        return MISS

    add_interceptor(scrubber)
    try:
        client.embeddings.create(model="text-embedding-3-small", input="bob@acme.com")
    finally:
        remove_interceptor(scrubber)
    assert kw["input"] == "[email]"  # str input stays str, content scrubbed
    call = [e for e in events if isinstance(e, LLMCall)][0]
    assert call.messages == [{"role": "user", "content": "[email]"}]

    kw.clear()
    add_interceptor(scrubber)
    try:
        client.embeddings.create(model="text-embedding-3-small", input=["bob@acme.com"])
    finally:
        remove_interceptor(scrubber)
    assert kw["input"] == ["[email]"]  # list input stays a list


def test_chat_capture_unaffected_by_embeddings_wrap(events):
    kw: dict = {}
    client = instrument(_embeddings_client(kw))
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    calls = [e for e in events if isinstance(e, LLMCall)]
    assert len(calls) == 1 and "embedding" not in calls[0].metadata
