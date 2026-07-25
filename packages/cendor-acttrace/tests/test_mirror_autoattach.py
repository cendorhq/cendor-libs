"""The audit mirror auto-attaches (DR-2a): governance is one line, not four.

`AuditLog(system=…)` is the *governance* line a user writes anyway. Under the telemetry switch its
**operational copy** now reaches the backend the app already configured — no `mirror=OTelMirror()`,
no telemetry code. `mirror=False` is the per-log opt-out; an explicit mirror is used verbatim.

What does NOT change: nothing ever creates an `AuditLog` for you (DR-2b), the chain is
untouched, and the hash-chained file remains the only thing `verify()` checks (rule 6).
"""

from __future__ import annotations

import pytest
from cendor.acttrace import AuditLog, OTelMirror, verify
from cendor.core import bus
from cendor.core.otel import TELEMETRY_ENV


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(TELEMETRY_ENV, raising=False)
    bus._reset()
    yield
    bus._reset()


class _ListMirror:
    def __init__(self) -> None:
        self.entries: list = []

    def write(self, entry) -> None:  # noqa: ANN001
        self.entries.append(entry)


def _audit_span_names(exporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans() if s.name.startswith("audit.")]


def test_a_bare_auditlog_puts_governance_on_the_wire(otel_traces):
    log = AuditLog(system="support")
    with log.decision(input="refund please") as d:
        d.human_oversight("ops", "approved")
    log.detach()
    names = _audit_span_names(otel_traces)
    assert "audit.audit_open" in names
    assert "audit.decision" in names
    assert "audit.human_oversight" in names


def test_mirror_false_never_mirrors(otel_traces):
    log = AuditLog(system="support", mirror=False)
    with log.decision(input="hi"):
        pass
    log.detach()
    assert _audit_span_names(otel_traces) == []


def test_an_explicit_mirror_is_used_verbatim(otel_traces):
    mine = _ListMirror()
    log = AuditLog(system="support", mirror=mine)
    with log.decision(input="hi"):
        pass
    log.detach()
    assert [e.type for e in mine.entries][:1] == ["audit_open"]
    assert _audit_span_names(otel_traces) == [], "an explicit mirror replaces the auto one entirely"


def test_off_never_attaches(otel_traces, monkeypatch):
    monkeypatch.setenv(TELEMETRY_ENV, "off")
    log = AuditLog(system="support")
    with log.decision(input="hi"):
        pass
    log.detach()
    assert _audit_span_names(otel_traces) == []


def test_without_otel_nothing_attaches_and_the_chain_is_unchanged(no_otel, tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="support", path=str(path))
    with log.decision(input="hi") as d:
        d.human_oversight("ops", "ok")
    log.detach()
    assert log._mirror is None, "no mirror is invented when OpenTelemetry is absent"
    ok, _ = verify(str(path))
    assert ok is True, "the evidence chain is byte-identical to a pre-switch release"


def test_the_auto_mirror_is_an_otelmirror(otel_traces):
    log = AuditLog(system="support")
    try:
        assert isinstance(log._mirror, OTelMirror)
    finally:
        log.detach()


def test_a_failing_auto_mirror_never_breaks_the_chain(otel_traces, tmp_path, monkeypatch):
    """The mirror is best-effort; auto-attaching must not change that guarantee."""

    def boom(self, entry):  # noqa: ANN001, ANN201
        raise RuntimeError("mirror down")

    monkeypatch.setattr(OTelMirror, "write", boom)
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="support", path=str(path))
    with log.decision(input="hi") as d:
        d.human_oversight("ops", "ok")
    log.detach()
    ok, _ = verify(str(path))
    assert ok is True


def test_detach_closes_the_auto_mirror(otel_traces):
    log = AuditLog(system="support")
    with log.decision(input="hi"):
        pass
    log.detach()  # must not raise — the auto mirror gets the same flush/close lifecycle
