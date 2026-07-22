"""GLR-6 — acttrace reads run/decision context from the *event* (captured pre-flight), not the
delivery-time ambient reads (F5/F6): a streamed call finalized outside the run/decision scope is
still chained under the right decision and joined to the right run; a BudgetEvent's trace_id flows
into the audit entry's run_id (the monitor's dual-key join)."""

from types import SimpleNamespace

from cendor.acttrace import AuditLog
from cendor.core import bus, instrument, trace
from cendor.core.ambient import _reset_ambient


def _streaming_client():
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)),
    ]

    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_out_of_scope_stream_keeps_decision_and_run(tmp_path):
    bus._reset()
    _reset_ambient()
    log = AuditLog(system="s", path=str(tmp_path / "a.jsonl"))
    client = _streaming_client()
    with trace("run-1"):
        with log.decision(input="x"):
            stream = client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "x"}], stream=True
            )
        # decision + trace scopes exit before the stream is drained
    list(stream)  # drain out of scope
    log.detach()
    llm = next(e for e in log.entries if e.type == "llm_call")
    decision = next(e for e in log.entries if e.type == "decision")
    assert llm.payload["decision_id"] == decision.payload["decision_id"]  # F5
    assert llm.payload["run_id"] == "run-1"  # F6


def test_budget_event_run_id_from_trace_id(tmp_path):
    bus._reset()
    _reset_ambient()
    log = AuditLog(system="s", path=str(tmp_path / "b.jsonl"))
    # Duck-typed BudgetEvent shape, emitted outside any trace scope: the run link must come from the
    # event's trace_id, not the (empty) delivery-time current_trace_id().
    bus.emit(
        SimpleNamespace(
            action="blocked",
            reason="cap",
            projected_usd="0.02",
            cap_usd="0.01",
            model="gpt-4o",
            tags={},
            trace_id="run-9",
        )
    )
    log.detach()
    be = next(e for e in log.entries if e.payload.get("action") == "blocked")
    assert be.payload["run_id"] == "run-9"
