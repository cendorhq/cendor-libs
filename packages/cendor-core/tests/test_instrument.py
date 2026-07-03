"""instrument(): mock clients only, no network. Idempotent wrap + normalized LLMCall events."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.types import LLMCall, Money, Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def _openai_client(prompt_tokens=100, completion_tokens=50):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                )
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _anthropic_async_client(input_tokens=10, output_tokens=20):
    class Messages:
        async def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
            )

    return SimpleNamespace(messages=Messages())


def test_sync_openai_emits_normalized_llmcall(events):
    client = instrument(_openai_client())
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    assert len(events) == 1
    call = events[0]
    assert isinstance(call, LLMCall)
    assert call.provider == "openai"
    assert call.model == "gpt-4o"
    assert call.usage == Usage(input_tokens=100, output_tokens=50)
    assert isinstance(call.cost, Money)
    # 0.0000025*100 + 0.00001*50 = 0.00075
    assert call.cost.amount == Decimal("0.00075")
    assert call.latency_ms is not None and call.latency_ms >= 0


def test_instrument_is_idempotent(events):
    client = _openai_client()
    instrument(client)
    first = client.chat.completions.create
    returned = instrument(client)
    assert returned is client
    assert client.chat.completions.create is first  # not double-wrapped

    client.chat.completions.create(model="gpt-4o", messages=[])
    assert len(events) == 1  # exactly one event per call, not two


async def test_async_anthropic_emits_event(events):
    client = instrument(_anthropic_async_client())
    await client.messages.create(
        model="claude-opus-4-8", messages=[{"role": "user", "content": "hi"}]
    )
    assert len(events) == 1
    assert events[0].provider == "anthropic"
    assert events[0].usage == Usage(input_tokens=10, output_tokens=20)


def test_anthropic_cache_reads_folded_into_input(events):
    # Anthropic reports input_tokens EXCLUDING cache reads. instrument() must normalize to the
    # documented subset convention: input includes cache reads, cached ⊆ input.
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100, output_tokens=50, cache_read_input_tokens=40
                )
            )

    client = instrument(SimpleNamespace(messages=Messages()))
    client.messages.create(model="claude-opus-4-8", messages=[{"role": "user", "content": "hi"}])
    call = events[0]
    assert call.provider == "anthropic"
    # input folds in the 40 cache reads (100 + 40); cached tracks them as a subset.
    assert call.usage == Usage(input_tokens=140, output_tokens=50, cached_tokens=40)
    assert call.usage.total_tokens == 190  # cached is a subset, not added on top
    # Matches Anthropic's real bill: (140-40)*input + 50*output + 40*cached
    # = 0.000005*100 + 0.000025*50 + 0.0000005*40 = 0.0005 + 0.00125 + 0.00002 = 0.00177
    assert call.cost.amount == Decimal("0.00177")


def test_anthropic_cache_creation_extracted_and_priced(events):
    # Anthropic cache_creation_input_tokens is a SEPARATE billed category (~1.25x input), not part
    # of input_tokens. instrument() captures it as Usage.cache_write and prices it.
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_input_tokens=40,
                    cache_creation_input_tokens=20,
                )
            )

    client = instrument(SimpleNamespace(messages=Messages()))
    client.messages.create(model="claude-opus-4-8", messages=[{"role": "user", "content": "hi"}])
    call = events[0]
    # input folds in cache reads (100+40); cache_write is separate, not in input or total.
    assert call.usage == Usage(input_tokens=140, output_tokens=50, cached_tokens=40, cache_write=20)
    assert call.usage.total_tokens == 190  # cache_write not added into total
    # (140-40)*input + 50*output + 40*cached + 20*cache_write
    # = 0.000005*100 + 0.000025*50 + 0.0000005*40 + 0.00000625*20
    # = 0.0005 + 0.00125 + 0.00002 + 0.000125 = 0.001895
    assert call.cost.amount == Decimal("0.001895")


def test_unknown_client_returned_untouched(events):
    sentinel = SimpleNamespace(foo="bar")
    assert instrument(sentinel) is sentinel


def test_unpriced_model_yields_no_cost(events):
    client = instrument(_openai_client())
    client.chat.completions.create(model="totally-unknown", messages=[])
    assert events[0].cost is None
    assert events[0].usage is not None  # usage still captured


def test_estimated_cost_is_labeled(events):
    client = instrument(_openai_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert events[0].metadata.get("cost_estimated") is True
    assert "cost_reported" not in events[0].metadata


def _openai_client_with_reported_cost(cost="0.00042"):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, cost=cost)
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_reported_cost_preferred_over_estimate(events):
    # a gateway (e.g. OpenRouter) reports usage.cost -> use it verbatim, not the table estimate
    client = instrument(_openai_client_with_reported_cost("0.00042"))
    client.chat.completions.create(model="gpt-4o", messages=[])
    call = events[0]
    assert call.cost == Money(Decimal("0.00042"))
    assert call.metadata.get("cost_reported") is True
    assert "cost_estimated" not in call.metadata


def _openai_stream_client(record):
    class Completions:
        def create(self, **kwargs):
            record.update(kwargs)
            # OpenAI emits a final usage chunk only when stream_options.include_usage is set
            usage_chunk = SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            )
            return iter([usage_chunk])

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_openai_stream_injects_usage_options_for_real_usage(events):
    rec: dict = {}
    client = instrument(_openai_stream_client(rec))
    stream = client.chat.completions.create(model="gpt-4o", stream=True, messages=[])
    list(stream)  # consume -> finalize emits the LLMCall once
    assert rec.get("stream_options") == {"include_usage": True}  # auto-injected
    assert len(events) == 1
    assert events[0].usage == Usage(input_tokens=10, output_tokens=5)
    assert events[0].metadata.get("usage_estimated") is not True  # real usage, not an estimate


def test_openai_stream_preserves_user_stream_options(events):
    rec: dict = {}
    client = instrument(_openai_stream_client(rec))
    list(
        client.chat.completions.create(
            model="gpt-4o", stream=True, messages=[], stream_options={"include_usage": False}
        )
    )
    assert rec["stream_options"] == {"include_usage": False}  # user's value left intact
