"""Newer client shapes: OpenAI Responses API + the google-genai SDK. Mock clients only, no network.

Mirrors the openai/anthropic mock tests in test_instrument.py for the shapes new 2026 apps use.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, tokens
from cendor.core.types import Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


# --------------------------------------------------------------------------- OpenAI Responses API


def _responses_client(reasoning=15, cached=20):
    class Responses:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    input_tokens_details=SimpleNamespace(cached_tokens=cached),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
                )
            )

    return SimpleNamespace(responses=Responses())


def test_responses_api_detected_and_priced(events):
    client = instrument(_responses_client())
    client.responses.create(model="gpt-4o", input="summarize this")
    call = events[0]
    assert call.provider == "openai"  # internal openai_responses surfaces as openai
    assert call.model == "gpt-4o"
    # Responses usage keys differ from chat-completions; reasoning + cached captured.
    assert call.usage == Usage(
        input_tokens=100, output_tokens=50, cached_tokens=20, reasoning_tokens=15
    )
    # cached ⊆ input, billed once: 0.0000025*(100-20) + 0.00001*50 + 0.00000125*20
    # = 0.0002 + 0.0005 + 0.000025 = 0.000725
    assert call.cost.amount == Decimal("0.000725")
    assert call.metadata.get("cost_estimated") is True


async def test_responses_api_async(events):
    class Responses:
        async def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    client = instrument(SimpleNamespace(responses=Responses()))
    await client.responses.create(model="gpt-4o", input="hi")
    assert events[0].provider == "openai"
    assert events[0].usage == Usage(input_tokens=10, output_tokens=5)


def test_responses_api_is_idempotent(events):
    client = _responses_client()
    instrument(client)
    first = client.responses.create
    instrument(client)
    assert client.responses.create is first  # not double-wrapped
    client.responses.create(model="gpt-4o", input="x")
    assert len(events) == 1


def test_both_openai_entrypoints_wrapped(events):
    # A real OpenAI client exposes chat.completions.create AND responses.create — wrap both.
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    class Responses:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=20, output_tokens=8))

    client = instrument(
        SimpleNamespace(chat=SimpleNamespace(completions=Completions()), responses=Responses())
    )
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "a"}])
    client.responses.create(model="gpt-4o", input="b")
    assert [e.usage for e in events] == [
        Usage(input_tokens=10, output_tokens=5),
        Usage(input_tokens=20, output_tokens=8),
    ]
    assert all(e.provider == "openai" for e in events)


def test_responses_api_streaming(events):
    # Responses streaming emits typed events; usage rides the completed event. And stream_options
    # (a Chat Completions param) must NOT be injected into a responses.create call.
    seen_kwargs = {}
    chunks = [
        SimpleNamespace(type="response.output_text.delta", delta="Hel"),
        SimpleNamespace(type="response.output_text.delta", delta="lo"),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                )
            ),
        ),
    ]

    class Responses:
        def create(self, **kwargs):
            seen_kwargs.update(kwargs)
            return iter(chunks)

    client = instrument(SimpleNamespace(responses=Responses()))
    got = list(client.responses.create(model="gpt-4o", input="hi", stream=True))
    assert got == chunks  # passthrough unchanged
    assert "stream_options" not in seen_kwargs  # not a Responses API param — never injected
    call = events[0]
    assert call.metadata["streamed"] is True
    assert not call.metadata.get("usage_estimated")  # real usage recovered from the completed event
    assert call.usage == Usage(input_tokens=100, output_tokens=50)


# --------------------------------------------------------------------------- google-genai SDK


def test_google_genai_sdk_sync(events):
    # New SDK: client.models.generate_content(model=…, contents=…) — model from the kwarg.
    class Models:
        def generate_content(self, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=20)
            )

    client = instrument(SimpleNamespace(models=Models()))
    client.models.generate_content(model="gemini-2.5-pro", contents="hello")
    call = events[0]
    assert call.provider == "google"
    assert call.model == "gemini-2.5-pro"  # read from model= kwarg (no GenerativeModel object)
    assert call.usage == Usage(input_tokens=40, output_tokens=20)
    assert call.cost is not None  # priced from the snapshot


async def test_google_genai_sdk_async(events):
    # Async path: client.aio.models.generate_content.
    class AioModels:
        async def generate_content(self, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(
                    prompt_token_count=30, candidates_token_count=10, thoughts_token_count=4
                )
            )

    client = instrument(SimpleNamespace(aio=SimpleNamespace(models=AioModels())))
    await client.aio.models.generate_content(model="gemini-1.5-pro", contents="hi")
    call = events[0]
    assert call.provider == "google"
    assert call.model == "gemini-1.5-pro"
    # thoughts fold into output and surface as reasoning (10 + 4)
    assert call.usage == Usage(input_tokens=30, output_tokens=14, reasoning_tokens=4)


def test_google_genai_sdk_wraps_sync_and_async(events):
    # A real genai.Client has both client.models and client.aio.models — wrap both.
    class Models:
        def generate_content(self, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1)
            )

    class AioModels:
        async def generate_content(self, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=2, candidates_token_count=2)
            )

    client = SimpleNamespace(models=Models(), aio=SimpleNamespace(models=AioModels()))
    instrument(client)
    assert client.models.generate_content is not Models.generate_content
    # both entrypoints are independently wrapped (idempotent, no double-wrap)
    first_sync = client.models.generate_content
    first_async = client.aio.models.generate_content
    instrument(client)
    assert client.models.generate_content is first_sync
    assert client.aio.models.generate_content is first_async


# --------------------------------------------------------------------------- non-dict message guard


def test_count_handles_non_dict_messages():
    # Gemini list-`contents` can be bare strings or SDK Part-like objects, not dicts. tokens.count
    # must not raise AttributeError on msg.get(...).
    assert tokens.count(["hello", "world"], model="gemini-1.5-pro") > 0
    part = SimpleNamespace(text="a thought")  # types.Part-like object
    assert tokens.count([part], model="gemini-1.5-pro") > 0
    # mixed shapes together
    assert tokens.count(["a", {"role": "user", "content": "b"}, part], model="gpt-4o") > 0


def test_gemini_stream_estimate_with_list_contents_does_not_raise(events):
    # The no-usage stream-estimate path counts call.messages; with Gemini list-contents that used to
    # hit msg.get(...) -> AttributeError. Now it estimates cleanly.
    class Models:
        def generate_content(self, **kwargs):
            # a stream with no usage_metadata anywhere -> forces the offline estimate path
            return iter([SimpleNamespace(text="partial")])

    client = instrument(SimpleNamespace(models=Models()))
    list(
        client.models.generate_content(
            model="gemini-1.5-pro", contents=["turn one", "turn two"], stream=True
        )
    )
    call = events[0]
    assert call.metadata.get("usage_estimated") is True
    assert call.usage is not None and call.usage.input_tokens > 0
