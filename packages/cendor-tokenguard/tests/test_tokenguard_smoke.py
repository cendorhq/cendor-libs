"""Public API presence + pre-flight estimate (no call, no network)."""

from decimal import Decimal

import pytest
from cendor import tokenguard
from cendor.core import tokens
from cendor.core.types import Money
from cendor.tokenguard import estimate


def test_public_api_present():
    for name in ("budget", "track", "estimate", "report", "BudgetExceeded", "reset"):
        assert hasattr(tokenguard, name)


def test_estimate_projects_cost_without_calling(monkeypatch):
    # Force the offline token heuristic so the projection is deterministic.
    monkeypatch.setattr(tokens, "_tiktoken_encoding", lambda model: None)
    msgs = [{"role": "user", "content": "hello world"}]  # gpt-4o -> 10 input tokens
    projected = estimate("gpt-4o", msgs, max_output_tokens=100)
    assert isinstance(projected, Money)
    # 0.0000025*10 + 0.00001*100 = 0.000025 + 0.001 = 0.001025
    assert projected.amount == Decimal("0.001025")


def test_estimate_unknown_model_raises():
    with pytest.raises(KeyError):
        estimate("nope", [{"role": "user", "content": "hi"}])
