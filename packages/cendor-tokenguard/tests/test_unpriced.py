"""Unpriced/unknown models must not let a USD budget silently no-op. Driven through the bus."""

from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument
from cendor.tokenguard import (
    BudgetExceeded,
    UnpricedModelWarning,
    budget,
    configure,
    report,
    unpriced_calls,
)

# A model id that is NOT in the bundled price table -> core leaves cost=None (a USD blind spot).
UNPRICED = "mystery-model-2099"


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    yield
    bus._reset()
    tokenguard.reset()


def _client(prompt_tokens=1000, completion_tokens=500):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                )
            )

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def _call(client, model=UNPRICED, n=1):
    for _ in range(n):
        client.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}])


def test_usd_block_unpriced_warns_and_proceeds_by_default():
    calls_made = {"n": 0}

    class Counting:
        def create(self, **kwargs):
            calls_made["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Counting())))

    with pytest.warns(UnpricedModelWarning, match="mystery-model-2099"):
        with budget(usd=0.01, on_exceed="block"):
            _call(client, n=2)

    assert calls_made["n"] == 2  # default on_unpriced="warn": calls proceed, cap can't bite


def test_usd_block_unpriced_raises_in_strict_mode():
    calls_made = {"n": 0}

    class Counting:
        def create(self, **kwargs):
            calls_made["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Counting())))
    configure(on_unpriced="raise")

    with pytest.raises(BudgetExceeded, match="no price"):
        with budget(usd=0.01, on_exceed="block"):
            _call(client, n=1)

    assert calls_made["n"] == 0  # strict mode rejects the unpriced call pre-flight, before it runs


def test_usd_raise_unpriced_warns_postflight():
    # "raise" is enforced post-flight (no pre-flight interceptor branch), so the warning fires from
    # the bus subscriber once the $0-cost call is recorded.
    client = _client()
    with pytest.warns(UnpricedModelWarning, match="on_exceed='raise'"):
        with budget(usd=0.01, on_exceed="raise"):
            _call(client, n=1)


def test_report_surfaces_unpriced_calls():
    client = _client()
    with pytest.warns(UnpricedModelWarning):
        with budget(usd=100.0):  # generous cap; point is the blind-spot accounting, not enforcement
            _call(client, n=3)

    assert unpriced_calls() == 3  # module-level count of $0-cost calls
    row = report(group_by=[]).rows[0]
    assert row["calls"] == 3
    assert row["unpriced_calls"] == 3
    assert row["usd"].amount == 0  # unpriced -> $0 recorded


def test_priced_model_does_not_warn_or_count_unpriced(recwarn):
    # False-positive guard: a priced model under a USD budget neither warns nor counts as unpriced.
    client = _client()
    with budget(usd=100.0, on_exceed="block"):
        _call(client, model="gpt-4o", n=2)

    assert not [w for w in recwarn.list if issubclass(w.category, UnpricedModelWarning)]
    assert unpriced_calls() == 0
    assert report(group_by=[]).rows[0]["unpriced_calls"] == 0


def test_token_cap_still_enforced_for_unpriced_model():
    # Token accounting is independent of pricing: a tokens= cap still trips on an unpriced model,
    # and (no USD cap) no unpriced warning is emitted.
    client = _client()  # 1500 tokens/call
    with pytest.raises(BudgetExceeded):
        with budget(tokens=2000, on_exceed="raise"):
            _call(client, n=10)


def test_warns_once_per_model():
    client = _client()
    with pytest.warns(UnpricedModelWarning) as record:
        with budget(usd=100.0):
            _call(client, n=5)
    unpriced_warnings = [w for w in record.list if issubclass(w.category, UnpricedModelWarning)]
    assert len(unpriced_warnings) == 1  # warn-once-per-model, not once-per-call


def test_configure_rejects_bad_on_unpriced():
    with pytest.raises(ValueError, match="on_unpriced"):
        configure(on_unpriced="explode")
