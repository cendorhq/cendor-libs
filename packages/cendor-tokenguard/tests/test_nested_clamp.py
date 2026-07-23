"""L2: on_exceed="clamp" injects a per-provider output ceiling — flat (OpenAI/Anthropic) and nested
(Bedrock inferenceConfig.maxTokens, Ollama options.num_predict, Gemini dict
config.max_output_tokens).
A typed Gemini config can't be safely merged, so it falls back to a hard block. No network."""

from types import SimpleNamespace

import cendor.tokenguard as tg
import pytest
from cendor.core import bus, instrument
from cendor.tokenguard import BudgetExceeded, budget, clamps


@pytest.fixture(autouse=True)
def _reset():
    bus._reset()
    tg.reset()
    yield
    bus._reset()
    tg.reset()


def _capture_kwargs(provider_client_factory):
    """Instrument a client whose call records the (possibly rerouted) kwargs it was invoked with."""
    seen = {}

    client = provider_client_factory(seen)
    return instrument(client), seen


def test_bedrock_clamp_injects_nested_max_tokens():
    seen = {}

    class Client:
        def converse(self, **kwargs):
            seen.update(kwargs)
            return {"usage": {"inputTokens": 3, "outputTokens": 2}}

    client = instrument(Client())
    with budget(tokens=500, on_exceed="clamp"):
        client.converse(
            modelId="claude-sonnet-4-6",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"temperature": 0.5},
        )
    assert "inferenceConfig" in seen
    assert seen["inferenceConfig"]["maxTokens"] > 0  # nested cap injected
    assert seen["inferenceConfig"]["temperature"] == 0.5  # copy-on-write: existing keys preserved
    assert any(c["kwarg"] == "inferenceConfig.maxTokens" for c in clamps())


def test_ollama_clamp_injects_num_predict():
    seen = {}

    class Client:
        def chat(self, **kwargs):
            seen.update(kwargs)
            return {"prompt_eval_count": 3, "eval_count": 2}

    client = instrument(Client())
    with budget(tokens=400, on_exceed="clamp"):
        client.chat(
            model="llama3",
            messages=[{"role": "user", "content": "hi"}],
            options={"temperature": 0.2},
        )
    assert seen["options"]["num_predict"] > 0
    assert seen["options"]["temperature"] == 0.2  # preserved
    assert any(c["kwarg"] == "options.num_predict" for c in clamps())


def test_gemini_dict_config_clamp_merges_max_output_tokens():
    seen = {}

    class Models:
        def generate_content(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2)
            )

    client = instrument(SimpleNamespace(models=Models()))
    with budget(tokens=300, on_exceed="clamp"):
        client.models.generate_content(
            model="gemini-1.5-pro", contents="hi", config={"temperature": 0.9}
        )
    assert seen["config"]["max_output_tokens"] > 0
    assert seen["config"]["temperature"] == 0.9  # preserved
    assert any(c["kwarg"] == "config.max_output_tokens" for c in clamps())


def test_gemini_typed_config_clamp_falls_back_to_block():
    # A typed GenerateContentConfig (not a dict) can't be safely merged -> hard block.
    class TypedConfig:  # stand-in for google.genai.types.GenerateContentConfig
        def __init__(self):
            self.temperature = 0.9

    class Models:
        def generate_content(self, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2)
            )

    client = instrument(SimpleNamespace(models=Models()))
    with budget(tokens=300, on_exceed="clamp"):
        with pytest.raises(BudgetExceeded, match="cannot fit call"):
            client.models.generate_content(
                model="gemini-1.5-pro", contents="hi", config=TypedConfig()
            )


def test_clamp_respects_callers_tighter_cap():
    seen = {}

    class Client:
        def converse(self, **kwargs):
            seen.update(kwargs)
            return {"usage": {"inputTokens": 3, "outputTokens": 2}}

    client = instrument(Client())
    with budget(tokens=5000, on_exceed="clamp"):
        client.converse(
            modelId="claude-sonnet-4-6",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 10},  # caller's own tighter cap
        )
    assert seen["inferenceConfig"]["maxTokens"] == 10  # left untouched (already fits)
    assert clamps() == []  # nothing injected
