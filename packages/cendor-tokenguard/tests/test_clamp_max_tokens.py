"""`on_exceed="clamp"` must not break a call that already sets `max_tokens` (OpenAI).

Found live on 2026-07-26 while seeding the fit-gap verification: a plain OpenAI call with
`max_tokens=4` inside a `budget(tokens=…, on_exceed="clamp")` scope returned

    400 — Setting 'max_tokens' and 'max_completion_tokens' at the same time is not supported

because the clamp read only `max_completion_tokens` as the caller's existing cap and injected that
kwarg regardless. Two bugs in one: the caller's own cap was ignored (so the clamp always injected),
and the injected kwarg collided with theirs. OpenAI takes either spelling and rejects both together;
`_projected_output` already accepted either, so the injection simply disagreed with it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.tokenguard import budget, reset


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    reset()
    yield
    bus._reset()
    reset()


def _client(sink: list[dict]):
    """A fake OpenAI-shaped client that RAISES on the real API's mutually-exclusive combination."""

    class Completions:
        def create(self, **kwargs):
            sink.append(kwargs)
            if "max_tokens" in kwargs and "max_completion_tokens" in kwargs:
                raise ValueError(
                    "Setting 'max_tokens' and 'max_completion_tokens' at the same time"
                    " is not supported"
                )
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_clamp_reuses_the_callers_max_tokens_instead_of_adding_the_other_name():
    seen: list[dict] = []
    client = instrument(_client(seen))
    with budget(tokens=4000, on_exceed="clamp", name="cap"):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], max_tokens=4
        )
    sent = seen[-1]
    assert "max_completion_tokens" not in sent, (
        f"clamp added the mutually-exclusive kwarg beside the caller's own: {sorted(sent)}"
    )
    # 4 already fits the 4000-token budget, so the caller's cap is left exactly as they set it.
    assert sent["max_tokens"] == 4


def test_a_callers_max_tokens_ABOVE_the_budget_is_tightened_in_place():
    seen: list[dict] = []
    client = instrument(_client(seen))
    with budget(tokens=60, on_exceed="clamp", name="cap"):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], max_tokens=5000
        )
    sent = seen[-1]
    assert "max_completion_tokens" not in sent
    assert 0 < sent["max_tokens"] < 5000, f"the cap was not tightened: {sent['max_tokens']}"


def test_max_completion_tokens_still_wins_when_the_caller_used_the_newer_name():
    seen: list[dict] = []
    client = instrument(_client(seen))
    with budget(tokens=60, on_exceed="clamp", name="cap"):
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            max_completion_tokens=5000,
        )
    sent = seen[-1]
    assert "max_tokens" not in sent
    assert 0 < sent["max_completion_tokens"] < 5000


def test_neither_name_set_still_injects_the_modern_kwarg():
    seen: list[dict] = []
    client = instrument(_client(seen))
    with budget(tokens=60, on_exceed="clamp", name="cap"):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    assert "max_completion_tokens" in seen[-1]
    assert "max_tokens" not in seen[-1]
