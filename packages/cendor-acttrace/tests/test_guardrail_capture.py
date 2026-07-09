"""acttrace chains a guardrail decision by duck typing — no import of cendor-guardrails.

Mirrors the contextkit `AssemblyReport` pattern: a bus event carrying ``guardrail``/``stage``/
``action`` becomes a tamper-evident ``guardrail_decision`` entry. We emit a plain SimpleNamespace
with those attributes so the test proves the *shape* contract, not a package dependency.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.acttrace import AuditLog, verify
from cendor.core import bus


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


def _decision(**kw):
    base = {
        "guardrail": "keyword_deny",
        "stage": "input",
        "action": "block",
        "reason": "denied keyword: 'bomb'",
        "agent": "triage",
        "tool": "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_guardrail_decision_is_chained_as_its_own_entry_type(tmp_path):
    path = tmp_path / "g.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        bus.emit(_decision())
    finally:
        log.detach()

    entries = [e for e in log.entries if e.type == "guardrail_decision"]
    assert len(entries) == 1
    payload = entries[0].payload
    assert payload["guardrail"] == "keyword_deny"
    assert payload["stage"] == "input"
    assert payload["action"] == "block"
    assert payload["reason"] == "denied keyword: 'bomb'"
    assert payload["agent"] == "triage"
    assert verify(str(path))[0] is True  # the decision is inside the verified hash chain


def test_guardrail_decision_correlates_with_the_active_decision(tmp_path):
    path = tmp_path / "g2.jsonl"
    log = AuditLog(system="s", path=str(path))
    try:
        with log.decision(input="claim", actor="agent"):
            bus.emit(_decision(action="flag", reason="matched"))
    finally:
        log.detach()

    entry = next(e for e in log.entries if e.type == "guardrail_decision")
    assert entry.payload["decision_id"]  # tagged with the surrounding decision
    assert entry.payload["action"] == "flag"
    assert verify(str(path))[0] is True
