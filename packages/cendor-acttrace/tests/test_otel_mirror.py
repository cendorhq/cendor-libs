"""AuditLog mirror seam, OpenTelemetry mirror, correlation ids, and budget_event chaining.

The mirror is an *operational copy* — a failing mirror must never break the tamper-evident chain,
and the on-disk file stays the sole artifact verify() checks. The active-OTel tests are guarded by
`importorskip` so the default (no-OTel) env exercises the no-op path, like the rest of the stack.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.acttrace import AuditLog, OTelMirror, verify
from cendor.core import bus


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


class _ListMirror:
    """A minimal mirror: records each chained entry in memory and tracks close()."""

    def __init__(self) -> None:
        self.entries: list = []
        self.closed = False

    def write(self, entry) -> None:
        self.entries.append(entry)

    def close(self) -> None:
        self.closed = True


def test_mirror_receives_every_chained_entry(tmp_path):
    mirror = _ListMirror()
    path = tmp_path / "a.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=mirror)
    with log.decision(input="hi") as d:
        d.human_oversight("ops", "approved")
    log.detach()

    types = [e.type for e in mirror.entries]
    assert "audit_open" in types
    assert "decision" in types
    assert "human_oversight" in types
    assert mirror.closed is True  # detach() flushed/closed the mirror lifecycle
    # The mirror sees the SAME chained entries the file holds (in chain order).
    assert [e.seq for e in mirror.entries] == sorted(e.seq for e in mirror.entries)


def test_mirror_failure_never_breaks_the_chain(tmp_path):
    class _Boom:
        def write(self, entry):
            raise RuntimeError("mirror down")

    path = tmp_path / "b.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=_Boom())
    with log.decision(input="x"):
        pass
    log.detach()

    assert verify(str(path))[0] is True  # chain intact despite the mirror throwing on every write


def test_budget_event_is_chained_by_duck_typing(tmp_path):
    # A tokenguard BudgetEvent is chained without acttrace importing tokenguard (shape contract).
    path = tmp_path / "c.jsonl"
    log = AuditLog(system="s", path=str(path))
    ev = SimpleNamespace(
        action="blocked",
        reason="pre-flight block: projected $9.00 would exceed cap $5.00",
        model="gpt-4o",
        to_model=None,
        scope="session",
        projected_usd="9.00",
        cap_usd="5.00",
        projected_tokens=None,
        cap_tokens=None,
        tags={"feature": "refund_sync"},
    )
    try:
        bus.emit(ev)
    finally:
        log.detach()

    entry = next(e for e in log.entries if e.type == "budget_event")
    assert entry.payload["action"] == "blocked"
    assert entry.payload["cap_usd"] == "5.00"
    assert entry.payload["tags"]["feature"] == "refund_sync"
    assert verify(str(path))[0] is True  # the budget action is inside the verified chain


def test_otelmirror_is_safe_without_otel(tmp_path):
    # OTelMirror constructs + writes without raising whether or not OpenTelemetry is installed.
    path = tmp_path / "d.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=OTelMirror())
    with log.decision(input="hi"):
        pass
    log.detach()
    assert verify(str(path))[0] is True


# --------------------------------------------------------------------- active OpenTelemetry paths


def _memory_tracer():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_otelmirror_emits_a_span_per_entry(tmp_path):
    tracer, exporter = _memory_tracer()
    path = tmp_path / "e.jsonl"
    log = AuditLog(system="support", path=str(path), mirror=OTelMirror(tracer=tracer))
    with log.decision(input="refund") as d:
        d.human_oversight("ops@bank", "approved", "manual check")
    log.detach()

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "audit.decision" in names
    assert "audit.human_oversight" in names
    ho = next(s for s in spans if s.name == "audit.human_oversight")
    assert ho.attributes["cendor.audit.type"] == "human_oversight"
    assert ho.attributes["cendor.audit.reviewer"] == "ops@bank"
    assert ho.attributes["cendor.audit.system"] == "support"


def test_correlation_ids_stamped_when_a_span_is_active(tmp_path):
    tracer, _ = _memory_tracer()
    path = tmp_path / "f.jsonl"
    log = AuditLog(system="s", path=str(path))
    with tracer.start_as_current_span("run"):
        with log.decision(input="hi"):
            pass
    log.detach()

    entry = next(e for e in log.entries if e.type == "decision")
    assert "otel_trace_id" in entry.payload
    assert len(entry.payload["otel_trace_id"]) == 32  # 128-bit trace id, hex
    assert len(entry.payload["otel_span_id"]) == 16  # 64-bit span id, hex
    assert verify(str(path))[0] is True  # correlation ids are inside the hashed, verified payload


# ------------------------------------------------ V2 mirror completeness (G11/G12/G16 attributes)


def test_budget_event_mirror_carries_identity_and_numbers(tmp_path):  # G10 + G11
    tracer, exporter = _memory_tracer()
    path = tmp_path / "g11.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=OTelMirror(tracer=tracer))
    ev = SimpleNamespace(
        action="blocked",
        reason="projected $9.00 would exceed cap $5.00",
        name="per-run cap",
        description="hard ceiling per support run",
        model="gpt-4o",
        to_model=None,
        scope="session",
        projected_usd="9.00",
        cap_usd="5.00",
        projected_tokens=None,
        cap_tokens=None,
        tags={"feature": "refund_sync"},
    )
    try:
        bus.emit(ev)
    finally:
        log.detach()

    span = next(s for s in exporter.get_finished_spans() if s.name == "audit.budget_event")
    a = span.attributes
    assert a["cendor.audit.budget"] == "per-run cap"  # the budget name, as one clear attribute
    assert "cendor.audit.name" not in a  # NOT the generic name key (suppressed for budget_event)
    assert a["cendor.audit.description"] == "hard ceiling per support run"
    assert a["cendor.audit.scope"] == "session"
    assert a["cendor.audit.projected_usd"] == "9.00"  # money as a string (Decimal house rule)
    assert a["cendor.audit.cap_usd"] == "5.00"
    assert a["cendor.audit.tag.feature"] == "refund_sync"


def test_budget_event_token_caps_are_ints(tmp_path):  # G11 numeric token attrs
    tracer, exporter = _memory_tracer()
    path = tmp_path / "g11b.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=OTelMirror(tracer=tracer))
    ev = SimpleNamespace(
        action="blocked",
        reason="tokens",
        model="gpt-4o",
        projected_usd=None,
        cap_usd=None,
        projected_tokens=3000,
        cap_tokens=1600,
        tags={},
    )
    try:
        bus.emit(ev)
    finally:
        log.detach()
    a = next(s for s in exporter.get_finished_spans() if s.name == "audit.budget_event").attributes
    assert a["cendor.audit.projected_tokens"] == 3000
    assert a["cendor.audit.cap_tokens"] == 1600


def test_guardrail_decision_mirror_carries_agent_tool_and_policy(tmp_path):  # G12
    tracer, exporter = _memory_tracer()
    path = tmp_path / "g12.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=OTelMirror(tracer=tracer))
    ev = SimpleNamespace(
        guardrail="prompt_injection",
        stage="input",
        action="block",
        reason="injection detected",
        agent="triage",
        tool="",
        metadata={"severity": "high", "policy_version": "2", "policy_hash": "abc123"},
    )
    try:
        bus.emit(ev)
    finally:
        log.detach()
    a = next(
        s for s in exporter.get_finished_spans() if s.name == "audit.guardrail_decision"
    ).attributes
    assert a["cendor.audit.agent"] == "triage"
    assert (
        a["cendor.audit.severity"] == "high"
    )  # nested severity now reaches the span (the bug fix)
    assert a["cendor.audit.policy_version"] == "2"
    assert a["cendor.audit.policy_hash"] == "abc123"


def test_llm_call_mirror_carries_usage_latency_replayed(tmp_path):  # G12
    from cendor.core.types import LLMCall, Money, Usage

    tracer, exporter = _memory_tracer()
    path = tmp_path / "g12b.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=OTelMirror(tracer=tracer))
    call = LLMCall(
        id="1",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "x"}],
        usage=Usage(input_tokens=100, output_tokens=40, reasoning_tokens=10),
        cost=Money.zero(),
        latency_ms=123.0,
        metadata={"replayed": True},
    )
    try:
        bus.emit(call)
    finally:
        log.detach()
    a = next(s for s in exporter.get_finished_spans() if s.name == "audit.llm_call").attributes
    assert a["cendor.audit.input_tokens"] == 100
    assert a["cendor.audit.output_tokens"] == 40
    assert a["cendor.audit.reasoning_tokens"] == 10
    assert a["cendor.audit.latency_ms"] == 123.0
    assert a["cendor.audit.replayed"] is True


def test_context_assembly_mirror_carries_budget_and_block_counts(tmp_path):  # G16
    tracer, exporter = _memory_tracer()
    path = tmp_path / "g16.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=OTelMirror(tracer=tracer))
    ev = SimpleNamespace(
        model="gpt-4o",
        budget=8000,
        used=6500,
        decisions=[
            {"action": "kept"},
            {"action": "kept"},
            {"action": "compressed"},
            {"action": "dropped"},
        ],
    )
    try:
        bus.emit(ev)
    finally:
        log.detach()
    a = next(
        s for s in exporter.get_finished_spans() if s.name == "audit.context_assembly"
    ).attributes
    assert a["cendor.audit.budget_tokens"] == 8000  # distinct from a budget *name*
    assert a["cendor.audit.used_tokens"] == 6500
    assert a["cendor.audit.kept"] == 2
    assert a["cendor.audit.compressed"] == 1  # squeeze's indirect visibility
    assert a["cendor.audit.dropped"] == 1
    assert "cendor.audit.truncated" not in a  # zero counts are omitted


def test_audit_open_mirror_carries_risk_tier(tmp_path):  # G12
    tracer, exporter = _memory_tracer()
    path = tmp_path / "g12c.jsonl"
    log = AuditLog(system="s", path=str(path), risk_tier="high", mirror=OTelMirror(tracer=tracer))
    log.detach()
    a = next(s for s in exporter.get_finished_spans() if s.name == "audit.audit_open").attributes
    assert a["cendor.audit.risk_tier"] == "high"


def test_otel_span_id_exposed_as_attribute(tmp_path):  # G12 correlation
    tracer, exporter = _memory_tracer()
    path = tmp_path / "g12d.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=OTelMirror(tracer=tracer))
    with tracer.start_as_current_span("run"):
        with log.decision(input="hi"):
            pass
    log.detach()
    dec = next(s for s in exporter.get_finished_spans() if s.name == "audit.decision")
    assert len(dec.attributes["cendor.audit.otel_span_id"]) == 16  # the pivot target, now queryable
