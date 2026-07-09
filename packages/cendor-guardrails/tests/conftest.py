"""Shared fixtures: a clean bus per test and a decision collector. No network."""

from __future__ import annotations

import pytest
from cendor.core import bus
from cendor.guardrails import GuardrailDecision


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


@pytest.fixture
def decisions() -> list[GuardrailDecision]:
    """Collect every ``GuardrailDecision`` emitted on the bus during a test."""
    collected: list[GuardrailDecision] = []

    def _sub(event: object) -> None:
        if isinstance(event, GuardrailDecision):
            collected.append(event)

    bus.subscribe(_sub)
    return collected
