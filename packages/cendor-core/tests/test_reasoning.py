"""Reasoning-token accounting across providers + streaming. No network.

Reasoning tokens are a *subset of* output tokens: providers that report them separately (OpenAI's
``completion_tokens_details.reasoning_tokens``, Gemini's ``thoughts_token_count``) populate
``Usage.reasoning_tokens``; providers that fold thinking into ``output_tokens`` without a separate
count leave it 0. Cost is unaffected — reasoning is already billed inside ``output_tokens``.
"""

from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, prices
from cendor.core.types import Usage


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def test_reasoning_is_a_subset_of_output_not_added_to_total():
    u = Usage(input_tokens=200, output_tokens=1200, reasoning_tokens=1000)
    assert u.reasoning_tokens == 1000
    assert u.total_tokens == 1400  # 200 + 1200; reasoning lives inside output, not added on top


def test_openai_reasoning_tokens_extracted(events):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=200,
                    completion_tokens=1200,  # already includes the 1000 reasoning tokens
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=1000),
                )
            )

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])
    assert events[0].usage == Usage(input_tokens=200, output_tokens=1200, reasoning_tokens=1000)


@pytest.mark.parametrize("model", ["o3-mini", "o1-mini", "o3", "o4-mini", "gpt-5.4"])
def test_o_series_reasoning_tokens_are_captured_and_priced(events, model):
    """Reasoning capture must not depend on the model *name* — it is read off the response shape.

    Added 2026-07-28 as a **regression guard, not a fix**: an external report of "reasoning tokens
    are not captured for o3-mini" did not reproduce — this test was green the first time it ran.
    `_extract_usage` reads `completion_tokens_details.reasoning_tokens` for the whole OpenAI family
    with no model-name test anywhere, and `o3-mini` is in the price snapshot. Parametrized across
    the o-series (plus a GPT-5 reasoning id) so that if a name pattern is ever introduced here, it
    shows up as one id failing while its siblings pass instead of as a silent special case.
    """
    from decimal import Decimal

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=200,
                    completion_tokens=1200,  # already includes the 1000 reasoning tokens
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=1000),
                )
            )

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    client.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}])
    call = events[0]
    assert call.usage == Usage(input_tokens=200, output_tokens=1200, reasoning_tokens=1000)
    # Reasoning is billed inside output_tokens, so cost is the plain input+output estimate — the
    # model must be in the price table for that to be a real number, not zero.
    assert call.cost is not None
    assert call.cost.amount > Decimal("0")
    assert call.cost.amount == prices.estimate(model, 200, 1200).amount


def test_openai_without_reasoning_details_stays_zero(events):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])
    assert events[0].usage.reasoning_tokens == 0


def test_gemini_thoughts_folded_into_output_and_surfaced(events):
    # Gemini reports thoughts_token_count *separately* from candidates_token_count; both bill as
    # output, so thoughts must be added to the output total (else a thinking model is under-costed)
    # and also surfaced as reasoning_tokens.
    class GenerativeModel:
        model_name = "models/gemini-1.5-pro"

        def generate_content(self, contents, **kwargs):
            return SimpleNamespace(
                usage_metadata=SimpleNamespace(
                    prompt_token_count=40, candidates_token_count=20, thoughts_token_count=80
                )
            )

    client = instrument(GenerativeModel())
    client.generate_content("hello")
    assert events[0].usage == Usage(input_tokens=40, output_tokens=100, reasoning_tokens=80)


def test_anthropic_reasoning_stays_zero(events):
    # Anthropic folds thinking into output_tokens with no separate count — reasoning stays 0,
    # but the total output (which already includes thinking) is still correct for cost.
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=50, output_tokens=300, cache_read_input_tokens=0)
            )

    client = instrument(SimpleNamespace(messages=Messages()))
    client.messages.create(model="claude-opus-4-8", messages=[{"role": "user", "content": "x"}])
    assert events[0].usage.output_tokens == 300
    assert events[0].usage.reasoning_tokens == 0


def test_real_openai_sdk_usage_models_are_read_not_just_our_fakes(events):
    """The same capture, driven by openai-python's OWN models — not a ``SimpleNamespace``.

    Added 2026-07-31 (GAPCLOSE Q3). The guard above was green from the day it was written, and the
    external report kept recurring anyway — because every test in this file builds the response out
    of ``SimpleNamespace``, which proves the field *names* we chose are read and nothing about the
    shapes the shipped SDK actually returns. A pydantic model with a renamed field, an alias, or a
    ``None`` default would sail straight through a fake and fail in production.

    Measured with this test's own shapes in
    ``plan/evidence-gapclose-2026-07-31/q3_probe_reasoning_extraction.py``: extraction is correct in
    both entrypoints, so the o3-mini report was never a library defect — the probe that raised it
    could not tell "core failed to read the field" from "the model reported nothing to read", and
    attributed the second to the library. Fixed on the testsuits side; pinned here.
    """
    from cendor.core.instrument import _extract_usage

    completion_usage = pytest.importorskip("openai.types.completion_usage")
    response_usage = pytest.importorskip("openai.types.responses.response_usage")

    # --- Chat Completions: completion_tokens_details.reasoning_tokens ---
    chat_usage = completion_usage.CompletionUsage(
        prompt_tokens=21,
        completion_tokens=100,
        total_tokens=121,
        completion_tokens_details=completion_usage.CompletionTokensDetails(
            accepted_prediction_tokens=0,
            audio_tokens=0,
            reasoning_tokens=64,
            rejected_prediction_tokens=0,
        ),
        prompt_tokens_details=completion_usage.PromptTokensDetails(audio_tokens=0, cached_tokens=8),
    )
    u = _extract_usage(SimpleNamespace(usage=chat_usage), "openai")
    assert u == Usage(input_tokens=21, output_tokens=100, cached_tokens=8, reasoning_tokens=64)

    # --- Responses API: output_tokens_details.reasoning_tokens ---
    resp_usage = response_usage.ResponseUsage(
        input_tokens=21,
        output_tokens=100,
        total_tokens=121,
        input_tokens_details=response_usage.InputTokensDetails(cached_tokens=0),
        output_tokens_details=response_usage.OutputTokensDetails(reasoning_tokens=64),
    )
    u = _extract_usage(SimpleNamespace(usage=resp_usage), "openai_responses")
    assert u == Usage(input_tokens=21, output_tokens=100, reasoning_tokens=64)

    # --- NEGATIVE CONTROL: a details block with NO reasoning_tokens must read 0, and must never
    # pick up a neighbouring field (accepted_prediction / audio / rejected_prediction) instead.
    odd = completion_usage.CompletionUsage(
        prompt_tokens=21,
        completion_tokens=100,
        total_tokens=121,
        completion_tokens_details=completion_usage.CompletionTokensDetails(
            accepted_prediction_tokens=99, audio_tokens=7, rejected_prediction_tokens=5
        ),
    )
    u = _extract_usage(SimpleNamespace(usage=odd), "openai")
    assert u is not None
    assert u.reasoning_tokens == 0, "a sibling details field was misread as reasoning"

    # --- NEGATIVE CONTROL: a genuine zero from a reasoning model is 0, not a missing value. The
    # documented convention is that `reasoning_tokens == 0` means "not reported separately" OR
    # "reported as zero" — Usage.reasoning_tokens is an int by design, so these are one value.
    zero = completion_usage.CompletionUsage(
        prompt_tokens=21,
        completion_tokens=5,
        total_tokens=26,
        completion_tokens_details=completion_usage.CompletionTokensDetails(reasoning_tokens=0),
    )
    u = _extract_usage(SimpleNamespace(usage=zero), "openai")
    assert u is not None
    assert u.reasoning_tokens == 0


def test_openai_streaming_recovers_reasoning(events):
    # The final usage chunk (stream_options include_usage) carries completion_tokens_details.
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hi"))], usage=None),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=900,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=800),
            ),
        ),
    ]

    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    list(
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
    )
    assert events[0].usage == Usage(input_tokens=100, output_tokens=900, reasoning_tokens=800)
    assert events[0].metadata["streamed"] is True
