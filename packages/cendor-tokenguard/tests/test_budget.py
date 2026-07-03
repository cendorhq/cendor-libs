"""Budget enforcement via the core event bus — pre-flight raise / truncate, no network.

We drive the bus with mock-instrumented clients so spend is real but no API is called.
"""

from types import SimpleNamespace

import pytest
from cendor import tokenguard
from cendor.core import bus, instrument
from cendor.tokenguard import BudgetExceeded, budget, track


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    tokenguard.reset()
    yield
    bus._reset()
    tokenguard.reset()


def _client(prompt_tokens=1000, completion_tokens=500):
    """gpt-4o: 0.0000025*1000 + 0.00001*500 = 0.0075 USD per call; 1500 tokens per call."""

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                )
            )

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def _call(client, n=1):
    for _ in range(n):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])


def test_raise_stops_a_runaway_loop():
    calls_made = {"n": 0}

    class Counting:
        def create(self, **kwargs):
            calls_made["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Counting())))

    with pytest.raises(BudgetExceeded):
        with budget(usd=0.01, on_exceed="raise"):
            _call(client, n=100)  # would cost $0.75 if it ran to completion

    # Cap is $0.01; each call is $0.0075. Call 1 -> $0.0075 (ok), call 2 -> $0.015 (trips).
    assert calls_made["n"] == 2


def test_block_prevents_the_overbudget_call_preflight():
    calls_made = {"n": 0}

    class Counting:
        def create(self, **kwargs):
            calls_made["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Counting())))

    with pytest.raises(BudgetExceeded):
        with budget(usd=0.01, on_exceed="block"):
            _call(client, n=100)

    # Unlike "raise" (which lets the breaching call complete, n==2), "block" is pre-flight: call 1
    # fits the projection and runs; call 2's projection would breach the cap, so it never executes.
    assert calls_made["n"] == 1


def test_under_budget_does_not_raise():
    client = _client()
    with budget(usd=1.00, on_exceed="raise"):
        _call(client, n=3)  # $0.0225 total
    assert tokenguard.report().total().amount  # spend recorded


def test_truncate_degrades_gracefully_in_decorator():
    client = _client()

    @budget(usd=0.01, on_exceed="truncate")
    def runaway():
        _call(client, n=100)
        return "completed"

    assert runaway() is None  # degraded, did not raise, did not complete


def test_truncate_in_context_manager_exits_cleanly():
    client = _client()
    with budget(usd=0.01, on_exceed="truncate"):
        _call(client, n=100)
    # No exception escaped; spend up to the trip point is recorded.
    assert tokenguard.report().total().amount > 0


def test_token_budget_trips():
    client = _client()  # 1500 tokens/call
    with pytest.raises(BudgetExceeded):
        with budget(tokens=2000, on_exceed="raise"):
            _call(client, n=10)


def test_callable_on_exceed_is_invoked():
    client = _client()
    fired = []
    with budget(usd=0.01, on_exceed=lambda ctx: fired.append(ctx["spent_usd"])):
        _call(client, n=3)
    assert fired  # callback ran instead of raising


async def test_budget_and_track_work_around_async_calls():
    # @budget wraps async functions; track() rides contextvars across awaits in the same task.
    class Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    @budget(usd=1.00)
    async def handle():
        with track(feature="async_bot"):
            return await client.chat.completions.create(model="gpt-4o", messages=[])

    await handle()
    rows = tokenguard.report(group_by=["feature"]).rows
    assert rows and rows[0]["tags"] == {"feature": "async_bot"} and rows[0]["calls"] == 1


def test_downgrade_reroutes_to_cheaper_model_preflight():
    # Tiny cap so the projection trips immediately: every call is rerouted BEFORE it runs.
    models = []

    class Completions:
        def create(self, **kwargs):
            models.append(kwargs["model"])
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    with budget(usd=0.001, on_exceed="downgrade", downgrade={"gpt-4o": "gpt-4o-mini"}):
        for _ in range(3):
            _call(client, n=1)  # requests gpt-4o each time

    assert models == ["gpt-4o-mini"] * 3  # rerouted to the cheaper model pre-flight, never raised
    dg = tokenguard.downgrades()
    assert len(dg) == 3
    assert dg[0]["from"] == "gpt-4o" and dg[0]["to"] == "gpt-4o-mini"


def test_no_downgrade_when_under_budget():
    models = []

    class Completions:
        def create(self, **kwargs):
            models.append(kwargs["model"])
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    with budget(usd=100.0, on_exceed="downgrade", downgrade={"gpt-4o": "gpt-4o-mini"}):
        _call(client, n=1)
    assert models == ["gpt-4o"]  # comfortably under budget -> no reroute
    assert tokenguard.downgrades() == []


def test_nested_budgets_inner_cap_trips_first():
    client = _client()  # $0.0075/call
    with pytest.raises(BudgetExceeded):
        with budget(usd=5.0, scope="session"):
            with budget(usd=0.01):  # inner cap trips after 2 calls
                _call(client, n=10)


def test_outer_hard_cap_enforced_through_inner_downgrade():
    # An inner downgrade frame is a post-flight no-op; it must not mask the outer raise cap.
    client = _client()  # $0.0075/call
    with pytest.raises(BudgetExceeded):
        with budget(usd=0.006, on_exceed="raise"):  # outer hard cap
            with budget(usd=0.006, on_exceed="downgrade", downgrade={"gpt-4o": "gpt-4o-mini"}):
                _call(client, n=2)  # $0.015 — over both caps; outer must still trip


def test_budget_rejects_invalid_config():
    with pytest.raises(ValueError):
        budget(usd=1.0, on_exceed="blok")  # typo in on_exceed
    with pytest.raises(ValueError):
        budget(on_exceed="raise")  # no cap at all -> would be a silent no-op
    with pytest.raises(ValueError):
        budget(usd=1.0, on_exceed="downgrade")  # downgrade with no map -> no protection
    with pytest.raises(ValueError):
        budget(tokens=100, on_exceed="downgrade", downgrade={"a": "b"})  # downgrade needs a usd cap


def test_output_reserve_makes_block_more_conservative():
    # A large output reserve makes the pre-flight projection breach sooner, blocking the first call.
    calls_made = {"n": 0}

    class Counting:
        def create(self, **kwargs):
            calls_made["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Counting())))
    with pytest.raises(BudgetExceeded):
        # 100k projected output * $0.00001 ≈ $1.0 > $0.50 cap -> blocked before running
        with budget(usd=0.50, on_exceed="block", output_reserve=100_000):
            _call(client, n=1)
    assert calls_made["n"] == 0  # blocked pre-flight thanks to the larger reserve


def test_max_tokens_zero_is_honored_not_treated_as_unset():
    # max_tokens=0 is an explicit cap (project 0 output), not "unset" falling back to the reserve.
    from cendor.core.types import LLMCall
    from cendor.tokenguard import _projected_output

    call = LLMCall(id="1", provider="openai", model="gpt-4o", messages=[])
    call.metadata["request_kwargs"] = {"max_tokens": 0}
    assert _projected_output(call, reserve=256) == 0  # 0 honored, not treated as falsy -> 256

    call.metadata["request_kwargs"] = {"max_completion_tokens": 0}
    assert _projected_output(call, reserve=256) == 0

    call.metadata["request_kwargs"] = {}  # no cap -> falls back to the reserve
    assert _projected_output(call, reserve=256) == 256


def _stream_delta(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


def _stream_usage(prompt, completion):
    return SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    )


def test_streaming_budget_fires_on_consumption_not_launch():
    # Contract: a streamed call is accounted when its stream is CONSUMED, not when launched. A
    # post-flight raise budget therefore can't trip until the stream is drained.
    chunks = [_stream_delta("hi"), _stream_usage(1000, 500)]  # $0.0075 for gpt-4o

    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    with budget(usd=0.001, on_exceed="raise"):  # any real call would breach this cap
        stream = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        # Launched but not consumed: nothing recorded yet, so the breaker has not fired.
        assert tokenguard.report().total().amount == 0
        # Draining the stream records the spend and trips the post-flight breaker at that moment.
        with pytest.raises(BudgetExceeded):
            list(stream)
