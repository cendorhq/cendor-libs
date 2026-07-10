"""Reasoning-model handling in tokenguard: clamp, reasoning_reserve, the max_completion_tokens
projection fix, and reasoning surfaced in report()/sinks. Driven through the core bus; no network.
"""

from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument
from cendor.tokenguard import BudgetExceeded, budget, clamps, report
from cendor.tokenguard.sinks import SQLiteSink


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    yield
    bus._reset()
    tokenguard.reset()


def _openai(usage: dict, seen_kwargs: list):
    """An instrumented OpenAI-shaped client that records the kwargs each call received."""

    class Completions:
        def create(self, **kwargs):
            seen_kwargs.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(**usage))

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_clamp_requires_a_tokens_cap():
    with pytest.raises(ValueError, match="clamp"):
        budget(usd=0.01, on_exceed="clamp")


def test_clamp_always_injects_a_ceiling_bounding_each_call():
    # M1: clamp must ALWAYS hand the provider a max_completion_tokens ceiling = the tokens left in
    # the budget — even a call that looks small pre-flight — so a surprise-long completion can't
    # overshoot the tokens= cap. (Old bug: with headroom no ceiling was injected and the call ran
    # uncapped; the reserve heuristic gated the injection.)
    seen: list = []
    client = _openai(
        {
            "prompt_tokens": 100,
            "completion_tokens": 850,  # ~950 tokens/call
            "completion_tokens_details": SimpleNamespace(reasoning_tokens=800),
        },
        seen,
    )
    with budget(tokens=1000, on_exceed="clamp"):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    rows = clamps()
    assert len(rows) == 2  # BOTH calls capped server-side (not just the one that runs low)
    assert all(r["kwarg"] == "max_completion_tokens" for r in rows)
    # First call: ceiling ~= full remaining budget minus the tiny input, and it reached the client.
    assert seen[0]["max_completion_tokens"] == rows[0]["limit"]
    assert 0 < rows[0]["limit"] <= 1000
    # Second call: the budget is mostly spent, so a much tighter ceiling.
    assert seen[1]["max_completion_tokens"] == rows[1]["limit"]
    assert rows[1]["limit"] < rows[0]["limit"]


def test_clamp_caps_a_single_oversized_call_even_with_headroom():
    # M1 (the finding's exact repro): a budget with plenty of headroom and one tiny-looking call
    # that returns far more than the 256-token reserve. Clamp must still inject a ceiling <= the
    # remaining budget so the single call cannot overshoot (old bug: injected nothing, uncapped).
    seen: list = []
    client = _openai({"prompt_tokens": 5, "completion_tokens": 5}, seen)
    with budget(tokens=1200, on_exceed="clamp"):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    assert len(clamps()) == 1
    injected = seen[0].get("max_completion_tokens")
    assert injected is not None and injected <= 1200


def test_clamp_falls_back_to_block_on_unsupported_provider():
    # Ollama puts the cap in nested options, so clamp can't inject it safely -> pre-flight block.
    class OllamaClient:
        def chat(self, **kwargs):
            return {"prompt_eval_count": 3, "eval_count": 5}

    client = instrument(OllamaClient())
    with pytest.raises(BudgetExceeded, match="clamp"):
        with budget(tokens=100, on_exceed="clamp"):
            client.chat(model="llama3", messages=[{"role": "user", "content": "hi"}])


def test_block_reads_max_completion_tokens():
    # Reasoning models pass max_completion_tokens (not max_tokens). The pre-flight projection must
    # honor it — before the fix it read only max_tokens and fell back to the 256 default.
    seen: list = []
    client = _openai({"prompt_tokens": 10, "completion_tokens": 5}, seen)
    with pytest.raises(BudgetExceeded, match="block"):
        with budget(tokens=500, on_exceed="block"):
            client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                max_completion_tokens=1000,  # 1000 > 500 cap -> must block pre-flight
            )
    assert seen == []  # the over-budget call never executed


def test_reasoning_reserve_tightens_an_uncapped_projection():
    # With no explicit output cap, reasoning_reserve adds headroom so the guard is conservative
    # about a reasoning model's hidden thinking.
    seen: list = []
    client = _openai({"prompt_tokens": 10, "completion_tokens": 5}, seen)
    with pytest.raises(BudgetExceeded, match="block"):
        with budget(tokens=5000, on_exceed="block", reasoning_reserve=10000):
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
            )
    assert seen == []


def test_report_and_sink_surface_reasoning_tokens(tmp_path):
    sink = SQLiteSink(str(tmp_path / "spend.db"))
    tokenguard.use_sink(sink)
    client = _openai(
        {
            "prompt_tokens": 100,
            "completion_tokens": 1200,
            "completion_tokens_details": SimpleNamespace(reasoning_tokens=1000),
        },
        [],
    )
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])

    row = report().rows[0]
    assert row["reasoning_tokens"] == 1000
    assert row["output_tokens"] == 1200
    assert row["tokens"] == 100 + 1200  # reasoning is a subset of output — not double-counted

    persisted = sink.rows()[0]  # (tags, usd, input_tokens, output_tokens, reasoning_tokens, model)
    assert persisted[4] == 1000
    sink.close()
