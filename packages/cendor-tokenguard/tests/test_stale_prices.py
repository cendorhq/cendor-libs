"""tokenguard: the warn-once stale-price-table signal (live-pricing wave, D6).

A USD cap enforced against stale rates is quietly wrong, and the direction depends on which way
prices moved: after a price CUT the estimate is high and the cap binds early (conservative); after
a price RISE it is low and the cap binds LATE — you overspend. That second case is why this warning
exists. Driven through the bus, like every other tokenguard test.
"""

from __future__ import annotations

import datetime
import warnings
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument, prices
from cendor.tokenguard import StalePriceTableWarning, UnpricedModelWarning, budget, configure

TODAY = datetime.date.today()


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    prices._reset()
    yield
    bus._reset()
    tokenguard.reset()
    prices._reset()


def _client():
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def _call(client, model="gpt-4o", n=1):
    for _ in range(n):
        client.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}])


def _table(updated: str | None):
    """Install a price table with a chosen `_updated`, exactly as a refresh() would."""
    table: dict = {
        "models": {"gpt-4o": {"input": Decimal("0.0000025"), "output": Decimal("0.00001")}}
    }
    if updated is not None:
        table["_updated"] = updated
    prices._install(table, "refreshed", "feed", "https://example.invalid/prices.json")


def _days_ago(n: int) -> str:
    return (TODAY - datetime.timedelta(days=n)).isoformat()


def test_a_usd_budget_on_an_old_table_warns_once(recwarn):
    _table(_days_ago(400))
    client = _client()
    with budget(usd=100):
        _call(client, n=5)
    stale = [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]
    assert len(stale) == 1, f"warn ONCE per process, not per call — got {len(stale)}"
    msg = str(stale[0].message)
    assert _days_ago(400) in msg
    assert "binds LATE" in msg  # names the failure, not just the age
    assert "prices.refresh()" in msg  # and how to fix it


def test_a_fresh_table_says_nothing(recwarn):
    _table(_days_ago(2))
    with budget(usd=100):
        _call(_client())
    assert not [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]


def test_an_undatable_table_is_never_stale(recwarn):
    """litellm / openrouter / vercel publish no as-of date. Unmeasurable is not stale — inventing
    an age would be exactly the dishonesty this wave removes. They surface through
    `prices.source_name()` and `prices.explain()` instead."""
    _table(None)
    assert prices.age_days() is None
    with budget(usd=100):
        _call(_client())
    assert not [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]


def test_a_tokens_only_budget_says_nothing(recwarn):
    """A token cap does not depend on a price at all."""
    _table(_days_ago(400))
    with budget(tokens=1_000_000):
        _call(_client())
    assert not [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]


def test_no_budget_at_all_says_nothing(recwarn):
    _table(_days_ago(400))
    _call(_client())
    assert not [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]


def test_ignore_silences_it(recwarn):
    _table(_days_ago(400))
    configure(on_stale_prices="ignore")
    with budget(usd=100):
        _call(_client())
    assert not [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]


def test_the_threshold_moves(recwarn):
    _table(_days_ago(10))
    with budget(usd=100):
        _call(_client())
    assert not [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]

    configure(stale_prices_after_days=5)
    with budget(usd=100):
        _call(_client())
    assert [w for w in recwarn if issubclass(w.category, StalePriceTableWarning)]


def test_configure_rejects_a_bad_value():
    with pytest.raises(ValueError, match="on_stale_prices"):
        configure(on_stale_prices="raise")  # not a supported mode — it is warn or ignore
    with pytest.raises(ValueError, match="stale_prices_after_days"):
        configure(stale_prices_after_days=-1)


def test_an_unpriced_call_warns_about_the_price_not_the_age(recwarn):
    """The two signals name different failures and must not double-fire on one call."""
    _table(_days_ago(400))
    with budget(usd=100):
        _call(_client(), model="mystery-model-2099")
    kinds = {w.category for w in recwarn}
    assert UnpricedModelWarning in kinds
    assert StalePriceTableWarning not in kinds


def test_reset_re_arms_the_once_per_process_latch():
    """`recwarn` is deliberately NOT used here. Two identical warnings from one source line are
    also deduped by Python's own `__warningregistry__`, so a `recwarn`-based count cannot tell
    "our latch held" from "the interpreter deduped it" — and would pass either way. An explicit
    `simplefilter("always")` takes the interpreter out of the question, leaving only our latch."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _table(_days_ago(400))
        with budget(usd=100):
            _call(_client())
        assert len([w for w in caught if issubclass(w.category, StalePriceTableWarning)]) == 1
        assert tokenguard._warned_stale_prices is True

        tokenguard.reset()
        assert tokenguard._warned_stale_prices is False, "reset() must re-arm the latch"
        _table(_days_ago(400))
        with budget(usd=100):
            _call(_client())
        assert len([w for w in caught if issubclass(w.category, StalePriceTableWarning)]) == 2


def test_the_warning_is_filterable_like_any_other():
    _table(_days_ago(400))
    with warnings.catch_warnings():
        warnings.simplefilter("error", StalePriceTableWarning)
        with pytest.raises(StalePriceTableWarning):
            with budget(usd=100):
                _call(_client())
