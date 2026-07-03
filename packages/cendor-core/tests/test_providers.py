"""Provider breadth: Bedrock / Gemini / Ollama instrumentation + OTel ingestion. No network."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, otel, tokens
from cendor.core.types import LLMCall, Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def test_bedrock_converse_instrumented(events):
    class Client:
        def converse(self, **kwargs):
            return {"usage": {"inputTokens": 30, "outputTokens": 12}}

    client = instrument(Client())
    client.converse(
        modelId="claude-sonnet-4-6", messages=[{"role": "user", "content": [{"text": "hi"}]}]
    )
    call = events[0]
    assert call.provider == "bedrock"
    assert call.model == "claude-sonnet-4-6"
    assert call.usage == Usage(input_tokens=30, output_tokens=12)
    assert call.cost is not None  # priced from the bundled snapshot


def test_gemini_generate_content_instrumented(events):
    # The model id is bound to the GenerativeModel (model_name), not passed to generate_content —
    # instrument() reads it so the LLMCall carries a real, priceable model id.
    class GenerativeModel:
        model_name = "models/gemini-1.5-pro"

        def generate_content(self, contents, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=20)
            )

    client = instrument(GenerativeModel())
    client.generate_content("hello")  # no model kwarg — comes from the object
    call = events[0]
    assert call.provider == "google"
    assert call.model == "gemini-1.5-pro"  # captured from model_name, "models/" stripped
    assert call.usage == Usage(input_tokens=40, output_tokens=20)
    assert call.cost is not None  # now priceable (gemini-1.5-pro is in the snapshot)


def test_ollama_chat_instrumented(events):
    class Client:
        def chat(self, **kwargs):
            return {"prompt_eval_count": 7, "eval_count": 5}

    client = instrument(Client())
    client.chat(model="llama3", messages=[{"role": "user", "content": "hi"}])
    call = events[0]
    assert call.provider == "ollama"
    assert call.usage == Usage(input_tokens=7, output_tokens=5)
    assert call.cost.amount == Decimal("0")  # local model priced at 0


def test_family_detection_extended():
    assert tokens.family("gemini-1.5-pro") == "google"
    assert tokens.family("anthropic.claude-sonnet-4-6") == "anthropic"  # bedrock-prefixed id
    assert tokens.family("llama3") == "default"


def test_otel_ingest_emits_priced_llmcall(events):
    call = otel.ingest(
        {
            "gen_ai.system": "azure_ai_foundry",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
        }
    )
    assert isinstance(call, LLMCall)
    assert call.provider == "azure_ai_foundry"
    assert call.usage == Usage(input_tokens=1000, output_tokens=500)
    assert call.cost.amount == Decimal("0.0075")  # gpt-4o pricing
    assert events[0] is call  # joined the shared bus, just like an instrumented call
    assert call.metadata["source"] == "otel"


def test_otel_ingest_reads_cached_and_reasoning_tokens(events):
    call = otel.ingest(
        {
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
            "gen_ai.usage.cached_tokens": 200,
            "gen_ai.usage.reasoning_tokens": 100,
        }
    )
    # Managed-runtime capture keeps the cached/reasoning breakdown, not just input/output.
    assert call.usage == Usage(
        input_tokens=1000, output_tokens=500, cached_tokens=200, reasoning_tokens=100
    )
    # cached ⊆ input, billed once: 0.0000025*(1000-200) + 0.00001*500 + 0.00000125*200
    # = 0.002 + 0.005 + 0.00025 = 0.00725
    assert call.cost.amount == Decimal("0.00725")
