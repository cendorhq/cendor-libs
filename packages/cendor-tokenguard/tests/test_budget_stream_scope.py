"""GLR-5 (Bug A) red->green: a streamed call whose stream is drained **after** the ``budget`` /
``track`` scope has exited must still accrue spend, enforce the budget, and attribute by tag. Before
the fix, tokenguard read the frames/tags from contextvars at delivery time — empty for an
out-of-scope drain — so the spend was silently lost (and cumulative caps under ``block`` could be
overrun). The fix captures the frames (by reference) + tags at call initiation via the core ambient
seam.

The cross-call cumulative-``block`` bypass (§3b-2) needs the real SDK detached-drain runner, so
it is verified live in cendor-testsuits, not here.
"""

from types import SimpleNamespace

import cendor.tokenguard as tokenguard
import pytest
from cendor.core import instrument
from cendor.tokenguard import BudgetExceeded, budget, report, track


def _streaming_client():
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None),
        SimpleNamespace(
            choices=[], usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500)
        ),  # ~$0.0075 on gpt-4o
    ]

    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_out_of_scope_drain_accrues_and_attributes():
    tokenguard.reset()
    client = _streaming_client()
    with budget(usd=1.0) as handle:
        with track(user="u1"):
            stream = client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
            )
        # NOT drained inside the scopes — the SDK stream runner would drain it on a detached task.
    # Scopes exited: the budget/track contextvars are reset.
    list(stream)
    # Accrued to the frame the handle wraps (RED before the fix: $0):
    assert handle.spent.amount > 0
    # Attributed to the tag active at initiation (RED before the fix: user=None):
    u1 = next((r for r in report(group_by=["user"]).rows if r["tags"].get("user") == "u1"), None)
    assert u1 is not None
    assert u1["usd"].amount > 0
    tokenguard.reset()


def test_out_of_scope_drain_enforces_raise():
    tokenguard.reset()
    client = _streaming_client()
    with budget(usd=0.001, on_exceed="raise"):
        stream = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        # not drained here
    # RED before the fix: no raise (frames empty at drain → spend silently lost).
    with pytest.raises(BudgetExceeded):
        list(stream)
    tokenguard.reset()
