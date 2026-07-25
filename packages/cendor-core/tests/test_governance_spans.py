"""Option C (DR-2c): governance ENFORCEMENT as ordinary telemetry — no audit vocabulary.

A telemetry user wants to see what their stack decided: a budget that blocked a call, a guardrail
that tripped. Those now ride plain `governance.*` spans with `cendor.gov.*` attributes, emitted by
the same core emitter that renders call spans — so no `AuditLog`, no evidence-shaped object, and no
`audit.*` name anywhere.

Rails pinned here: the mirror wins when one is on the wire (never two renderings of one decision),
and **no payload-derived text reaches a `cendor.gov.*` attribute** (rule 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, otel


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(otel.TELEMETRY_ENV, raising=False)
    bus._reset()
    otel._reset_governance_mirrors()
    yield
    otel._reset_governance_mirrors()
    bus._reset()


# The two enforcement events, duck-typed exactly as tokenguard/guardrails emit them (core imports
# neither — rule 2 — so a local stand-in is the honest fixture).
@dataclass
class _BudgetEvent:
    action: str = "blocked"
    reason: str = "projected $0.0026 > cap $0.0000000010"
    name: str | None = "per-run cap"
    description: str | None = None
    model: str = "gpt-4o"
    to_model: str | None = None
    scope: str | None = "session"
    projected_usd: str | None = "0.002600000"
    cap_usd: str | None = "1E-9"
    projected_tokens: int | None = None
    cap_tokens: int | None = None
    tags: dict = field(default_factory=dict)
    trace_id: str = "run-42"
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _GuardrailDecision:
    guardrail: str = "keyword_deny"
    stage: str = "input"
    action: str = "block"
    reason: str = ""
    agent: str = "support"
    tool: str = ""
    trace_id: str = "run-42"
    metadata: dict = field(default_factory=dict)


def _client():
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _spans(exporter, prefix="governance."):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def test_a_budget_block_renders_a_governance_span(otel_traces):
    instrument(_client())  # arm the emitter — a zero-telemetry-code libs app
    bus.emit(_BudgetEvent())
    spans = _spans(otel_traces)
    assert [s.name for s in spans] == ["governance.budget_event"]
    a = spans[0].attributes
    assert a["cendor.gov.type"] == "budget_event"
    assert a["cendor.gov.action"] == "blocked"
    assert a["cendor.gov.budget"] == "per-run cap"
    assert a["cendor.gov.projected_usd"] == "0.002600000"
    assert a["cendor.gov.cap_usd"] == "1E-9"
    assert a["cendor.gov.model"] == "gpt-4o"
    assert a["cendor.gov.scope"] == "session"
    assert a["cendor.trace_id"] == "run-42", "correlates to the run exactly like a chat span"


def test_a_guardrail_decision_renders_a_governance_span(otel_traces):
    instrument(_client())
    bus.emit(_GuardrailDecision())
    spans = _spans(otel_traces)
    assert [s.name for s in spans] == ["governance.guardrail_decision"]
    a = spans[0].attributes
    assert a["cendor.gov.guardrail"] == "keyword_deny"
    assert a["cendor.gov.stage"] == "input"
    assert a["cendor.gov.action"] == "block"
    assert a["cendor.gov.agent"] == "support"
    assert "cendor.gov.tool" not in a, "empty fields are omitted, not stamped as ''"


def test_no_audit_vocabulary_anywhere_on_an_ops_span(otel_traces):
    """Rule 6: these are operational signals. `audit` must mean the evidence chain, nothing else."""
    instrument(_client())
    bus.emit(_BudgetEvent())
    bus.emit(_GuardrailDecision())
    for span in _spans(otel_traces):
        assert not span.name.startswith("audit.")
        assert not any(k.startswith("cendor.audit.") for k in span.attributes)


def test_no_reason_string_and_no_payload_text_reaches_a_gov_attr(otel_traces):
    """The pin the plan asks for: nothing input-derived on a default-on span.

    A guardrail's `reason` is written by the rule — and by a judge *model* for `rules.llm_judge`,
    which can paraphrase the payload. So `reason` is not emitted at all, and this test proves a
    payload marker cannot appear through any field.
    """
    instrument(_client())
    marker = "SSN-123-45-6789-SECRET"
    bus.emit(_GuardrailDecision(reason=f"the user said {marker}", metadata={"matched": marker}))
    bus.emit(_BudgetEvent(reason=marker, description=marker))
    for span in _spans(otel_traces):
        assert "cendor.gov.reason" not in span.attributes
        for key, value in span.attributes.items():
            assert marker not in str(value), f"{key} leaked payload-derived text"
            assert marker not in key


def test_the_audit_mirror_wins_while_one_is_attached(otel_traces):
    """An event must never render twice. While a mirror is on the wire, ops spans stand down."""
    instrument(_client())
    otel.governance_mirrored(True)  # what acttrace signals when a wire-mirror attaches
    bus.emit(_BudgetEvent())
    assert _spans(otel_traces) == []
    otel.governance_mirrored(False)
    bus.emit(_BudgetEvent())
    assert len(_spans(otel_traces)) == 1, "and they resume once the mirror detaches"


def test_the_mirror_refcount_composes_for_several_logs(otel_traces):
    instrument(_client())
    otel.governance_mirrored(True)
    otel.governance_mirrored(True)
    otel.governance_mirrored(False)
    bus.emit(_BudgetEvent())
    assert _spans(otel_traces) == [], "one log detaching must not re-open the ops path"
    otel.governance_mirrored(False)
    bus.emit(_BudgetEvent())
    assert len(_spans(otel_traces)) == 1


def test_off_kills_governance_spans_too(otel_traces, monkeypatch):
    monkeypatch.setenv(otel.TELEMETRY_ENV, "off")
    instrument(_client())
    bus.emit(_BudgetEvent())
    assert _spans(otel_traces) == []


def test_an_unrelated_event_renders_nothing(otel_traces):
    instrument(_client())
    bus.emit(SimpleNamespace(something="else"))
    assert _spans(otel_traces) == []


def test_a_live_spans_scope_defers_the_flat_rendering(otel_traces):
    """Inside an SDK run the SDK renders these as run children — core must not also render them."""
    instrument(_client())
    otel.enter_live_spans()
    bus.emit(_BudgetEvent())
    otel.exit_live_spans()
    assert _spans(otel_traces) == []
