"""tokenguard emits a BudgetEvent on the bus for each pre-flight budget action.

A blocked call never reaches the bus as an LLMCall (it's refused pre-flight), so the BudgetEvent is
the only signal the breaker fired — which is what acttrace chains and an OTel mirror alerts on. We
drive the bus with mock-instrumented clients so the enforcement is real but no API is called.
"""

from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument
from cendor.tokenguard import BudgetEvent, BudgetExceeded, budget, track


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    yield
    bus._reset()
    tokenguard.reset()


def _capture():
    """Subscribe a collector for BudgetEvents and return the list it fills."""
    events: list[BudgetEvent] = []
    bus.subscribe(lambda ev: events.append(ev) if isinstance(ev, BudgetEvent) else None)
    return events


def _client(prompt_tokens=1000, completion_tokens=500):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                )
            )

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def _call(client, model="gpt-4o"):
    client.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}])


def test_block_emits_a_blocked_budget_event():
    events = _capture()
    client = _client()
    with pytest.raises(BudgetExceeded):
        with budget(usd=0.01, on_exceed="block", scope="session"):
            _call(client)  # call 1 fits
            _call(client)  # call 2 projection breaches -> blocked pre-flight

    blocked = [e for e in events if e.action == "blocked"]
    assert len(blocked) == 1
    assert blocked[0].model == "gpt-4o"
    assert blocked[0].cap_usd == "0.01"
    assert blocked[0].scope == "session"
    assert blocked[0].projected_usd is not None


def test_token_block_emits_projected_tokens():
    events = _capture()
    client = _client()
    with pytest.raises(BudgetExceeded):
        with budget(tokens=1600, on_exceed="block"):
            _call(client)  # 1500 tokens fits
            _call(client)  # projection ~3000 > 1600 -> blocked

    blocked = [e for e in events if e.action == "blocked"]
    assert blocked and blocked[0].cap_tokens == 1600
    assert blocked[0].projected_tokens is not None


def test_downgrade_emits_a_downgraded_budget_event():
    events = _capture()
    client = _client()
    with budget(usd=0.001, on_exceed="downgrade", downgrade={"gpt-4o": "gpt-4o-mini"}):
        _call(client)

    downgraded = [e for e in events if e.action == "downgraded"]
    assert len(downgraded) == 1
    assert downgraded[0].model == "gpt-4o"
    assert downgraded[0].to_model == "gpt-4o-mini"


def test_clamp_emits_a_clamped_budget_event():
    events = _capture()
    client = _client()
    with budget(tokens=1200, on_exceed="clamp"):
        _call(client)  # input ~ small; clamp injects a max_completion_tokens ceiling

    clamped = [e for e in events if e.action == "clamped"]
    assert len(clamped) == 1
    assert clamped[0].cap_tokens == 1200


def test_budget_event_carries_active_tags():
    events = _capture()
    client = _client()
    with pytest.raises(BudgetExceeded):
        with track(feature="refund_sync", user_id="alice"):
            with budget(usd=0.01, on_exceed="block"):
                _call(client)
                _call(client)

    blocked = [e for e in events if e.action == "blocked"]
    assert blocked and blocked[0].tags.get("feature") == "refund_sync"


def test_no_budget_event_when_under_cap():
    events = _capture()
    client = _client()
    with budget(usd=100.0, on_exceed="block"):
        _call(client)
    assert [e for e in events if isinstance(e, BudgetEvent)] == []
