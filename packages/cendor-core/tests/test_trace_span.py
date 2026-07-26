"""`core.trace()` is a REAL span (core 1.14.0) — one scope is one trace.

Before this release the scope only stamped an ambient id, so every call inside still arrived as its
own root span: a scope around a chat call and a tool call produced TWO traces sharing one
`cendor.trace_id`. In a monitor that meant one logical unit of work rendered as two unrelated rows,
its governance fanned out to both, and per-run governance counts doubled — while the console told
users to reach for `core.trace()` to get a hierarchy it could not produce.

Rails these tests exist for (`plan/PLAN-MONITOR-FITGAP-REMEDIATION.md` §2):
  * rail 4 — every attribution claim is tested with TWO OVERLAPPING scopes and a client that takes
    real time. A zero-latency stub finishes scope A before scope B starts, so the process-wide bus
    never interleaves and every cross-scope defect is invisible.
  * rail 5 — a libs `trace()` inside an SDK run must not open a competing root, and a run-less libs
    call must not be adopted into a scope. Both directions.

No network: a fake client + the in-memory exporter fixtures from the workspace conftest.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, otel
from cendor.core.instrument import current_trace_id, instrument_tool, trace


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bus._reset()
    monkeypatch.delenv(otel.TELEMETRY_ENV, raising=False)
    monkeypatch.delenv(otel.TRACE_SPAN_ENV, raising=False)
    monkeypatch.delenv(otel.DEBUG_ENV, raising=False)
    yield
    bus._reset()


def _client(*, delay: float = 0.0, prompt: int = 10, completion: int = 4):
    """A fake chat client. `delay` makes the call take real time — the only way two scopes on two
    threads actually overlap (an instant stub serializes and proves nothing about attribution)."""

    class Completions:
        def create(self, **kwargs):
            if delay:
                time.sleep(delay)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _spans(exporter, prefix: str | None = None):
    out = list(exporter.get_finished_spans())
    if prefix is not None:
        out = [s for s in out if s.name.startswith(prefix)]
    return out


def _by_trace(spans):
    groups: dict[int, list] = {}
    for s in spans:
        groups.setdefault(s.context.trace_id, []).append(s)
    return groups


# ------------------------------------------------------------------ the headline: one scope, one
# trace


def test_a_scope_over_two_calls_is_ONE_trace_with_ordered_children(otel_traces):
    """The measured defect, inverted: chat + tool inside one scope = 1 trace, 1 parent, 2
    children."""
    client = instrument(_client())

    @instrument_tool("read_file")
    def read_file(p: str) -> str:
        return "ok"

    with trace("fs-tool"):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
        )
        read_file("a")

    spans = _spans(otel_traces)
    groups = _by_trace(spans)
    assert len(groups) == 1, (
        f"one scope must be ONE trace, got {len(groups)}: {[s.name for s in spans]}"
    )
    names = sorted(s.name for s in spans)
    assert names == ["cendor.trace fs-tool", "chat gpt-4o-mini", "execute_tool read_file"]

    parent = next(s for s in spans if s.name == "cendor.trace fs-tool")
    assert parent.parent is None, "the scope span is the root"
    assert parent.attributes["cendor.run.id"] == "fs-tool"
    assert parent.attributes["cendor.scope"] == "trace"
    for child in (s for s in spans if s is not parent):
        assert child.parent is not None and child.parent.span_id == parent.context.span_id, (
            f"{child.name} must be a CHILD of the scope span"
        )
    # …and the children are ordered, not left to timestamp luck.
    steps = {s.name: s.attributes.get("cendor.step") for s in spans if s is not parent}
    assert steps == {"chat gpt-4o-mini": 1, "execute_tool read_file": 2}
    # The ambient id is still stamped on every event — correlation by `cendor.trace_id` is
    # unaffected.
    assert all(s.attributes.get("cendor.trace_id") == "fs-tool" for s in spans if s is not parent)


def test_a_call_OUTSIDE_a_scope_is_still_its_own_root(otel_traces):
    """No scope ⇒ nothing changes. A flat governed call keeps being a flat governed call."""
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}])
    spans = _spans(otel_traces)
    assert [s.name for s in spans] == ["chat gpt-4o-mini"]
    assert spans[0].parent is None
    assert "cendor.step" not in spans[0].attributes, (
        "a step ordinal only means something in a scope"
    )
    assert not spans[0].attributes.get("cendor.trace_id")


def test_nesting_is_a_no_op_for_the_inner_scope(otel_traces):
    """One root per scope family: the inner `trace()` rebinds the id but opens no second span."""
    client = instrument(_client())
    with trace("outer"):
        with trace("inner"):
            client.chat.completions.create(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
            )
    spans = _spans(otel_traces)
    parents = [s for s in spans if s.name.startswith("cendor.trace ")]
    assert [p.name for p in parents] == ["cendor.trace outer"], "no second root for the inner scope"
    assert len(_by_trace(spans)) == 1
    # The ambient id still follows the innermost binding (unchanged, pre-1.14 behaviour).
    chat = next(s for s in spans if s.name.startswith("chat "))
    assert chat.attributes["cendor.trace_id"] == "inner"


# ------------------------------------------------------------------------- rail 4: OVERLAPPING
# scopes


def test_two_OVERLAPPING_scopes_each_render_their_own_call_exactly_once(otel_traces):
    """rail 4 — two scopes on two threads, with a client that takes real time so they genuinely
    interleave. `bus.emit` is a process-wide fanout, which is exactly how the 2026-07-25 wave
    shipped
    a cross-run attribution bug with every (sequential) probe green."""
    client = instrument(_client(delay=0.25))
    barrier = threading.Barrier(2)

    def work(scope_id: str, model: str):
        barrier.wait()  # start both scopes inside each other's window
        with trace(scope_id):
            client.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}])

    threads = [
        threading.Thread(target=work, args=("scope-a", "gpt-4o-mini")),
        threading.Thread(target=work, args=("scope-b", "claude-sonnet-5")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spans = _spans(otel_traces)
    groups = _by_trace(spans)
    assert len(groups) == 2, f"two scopes must be two traces, got {len(groups)}"
    # Every trace holds exactly its own scope span + its own single call — no double rendering, no
    # lost call, no shared run id.
    seen_ids = set()
    for members in groups.values():
        assert len(members) == 2, (
            f"expected scope + 1 call per trace, got {[m.name for m in members]}"
        )
        parent = next(m for m in members if m.name.startswith("cendor.trace "))
        child = next(m for m in members if m is not parent)
        rid = parent.attributes["cendor.run.id"]
        seen_ids.add(rid)
        assert child.attributes["cendor.trace_id"] == rid, (
            "a call was attributed to the OTHER scope"
        )
        assert child.parent.span_id == parent.context.span_id
        assert child.attributes["cendor.step"] == 1
    assert seen_ids == {"scope-a", "scope-b"}
    # Exactly two chat spans in total: neither call was rendered twice nor dropped.
    assert len(_spans(otel_traces, "chat ")) == 2


def test_a_scope_and_a_concurrent_UNSCOPED_call_do_not_contaminate_each_other(otel_traces):
    """rail 5, the second direction: a run-less call must not be adopted into an open scope."""
    client = instrument(_client(delay=0.25))
    barrier = threading.Barrier(2)

    def scoped():
        barrier.wait()
        with trace("scoped"):
            client.chat.completions.create(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
            )

    def unscoped():
        barrier.wait()
        client.chat.completions.create(
            model="gpt-4.1-nano", messages=[{"role": "user", "content": "y"}]
        )

    threads = [threading.Thread(target=scoped), threading.Thread(target=unscoped)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spans = _spans(otel_traces)
    loose = next(s for s in spans if s.name == "chat gpt-4.1-nano")
    assert loose.parent is None, "an unscoped call was ADOPTED into the concurrent scope"
    assert not loose.attributes.get("cendor.trace_id")
    assert "cendor.step" not in loose.attributes
    inside = next(s for s in spans if s.name == "chat gpt-4o-mini")
    assert inside.parent is not None and inside.attributes["cendor.trace_id"] == "scoped"


# ------------------------------------------------- rail 5: inside an SDK run, attach — never
# compete


def test_inside_an_sdk_run_the_scope_opens_NO_competing_root(otel_traces):
    """rail 5 — the SDK's `live_spans` already owns the run root and the run's trace. A second layer
    would put a `cendor.core`-scoped span inside a `cendor.sdk` trace, which is a door leak in any
    consumer that routes by instrumentation scope. The call attaches to the run's trace either
    way."""
    from opentelemetry import trace as ot

    client = instrument(_client())
    run_tracer = ot.get_tracer("cendor.sdk")
    otel.enter_live_spans()  # what the SDK's live_spans does to the core emitter
    try:
        with run_tracer.start_as_current_span("agent.run") as root:
            with trace("libs-inside-a-run"):
                # The core emitter stands down while live_spans owns the spans, so the SDK renders
                # the
                # call. What matters here is that NO `cendor.trace` root was opened beside the run.
                client.chat.completions.create(
                    model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
                )
            run_trace_id = root.context.trace_id
    finally:
        otel.exit_live_spans()

    spans = _spans(otel_traces)
    assert not [s for s in spans if s.name.startswith("cendor.trace ")], (
        "a libs trace() opened a competing root inside an SDK run"
    )
    assert len(_by_trace(spans)) == 1, "the run's trace must stay the only trace"
    assert spans[0].context.trace_id == run_trace_id
    # The ambient id is still stamped, so the SDK/acttrace correlation path is untouched.
    assert current_trace_id() == ""


# --------------------------------------------------------------------------------- the switches


def test_CENDOR_TRACE_SPAN_off_restores_the_pre_1_14_shape(otel_traces, monkeypatch):
    """The documented opt-out for an app whose backend groups by trace id today."""
    monkeypatch.setenv(otel.TRACE_SPAN_ENV, "off")
    assert otel.trace_span_enabled() is False
    client = instrument(_client())
    with trace("no-span"):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
        )
    spans = _spans(otel_traces)
    assert [s.name for s in spans] == ["chat gpt-4o-mini"]
    assert spans[0].parent is None, "with the switch off, the call is a root again"
    assert spans[0].attributes["cendor.trace_id"] == "no-span", "the ambient id is still stamped"


def test_the_span_can_be_forced_off_per_scope(otel_traces):
    client = instrument(_client())
    with trace("explicit-off", span=False):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
        )
    assert not [s for s in _spans(otel_traces) if s.name.startswith("cendor.trace ")]


def test_CENDOR_TELEMETRY_off_opens_no_scope_span(otel_traces, monkeypatch):
    monkeypatch.setenv(otel.TELEMETRY_ENV, "off")
    assert otel.trace_span_enabled() is False
    client = instrument(_client())
    with trace("telemetry-off"):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
        )
    assert not [s for s in _spans(otel_traces) if s.name.startswith("cendor.trace ")]


def test_no_provider_configured_opens_no_scope_span():
    """Local-first: with no global provider there is nobody listening, so nothing is emitted and the
    scope is exactly the pre-1.14 ContextVar."""
    assert otel.provider_configured() is False
    with trace("nobody-listening"):
        assert current_trace_id() == "nobody-listening"


def test_no_otel_installed_is_an_inert_no_op(no_otel):
    """The local-first rail: OpenTelemetry absent ⇒ `trace()` still stamps, and imports nothing."""
    with trace("offline"):
        assert current_trace_id() == "offline"
    assert otel.trace_span_enabled() is True  # the switch is on; there is simply no OTel to use


def test_an_exception_inside_the_scope_still_closes_it(otel_traces):
    client = instrument(_client())
    with pytest.raises(RuntimeError):
        with trace("boom"):
            client.chat.completions.create(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
            )
            raise RuntimeError("boom")
    spans = _spans(otel_traces)
    assert any(s.name == "cendor.trace boom" for s in spans), (
        "the scope span must still be exported"
    )
    assert current_trace_id() == "", "the ambient id is restored"
    # A second scope afterwards is not blocked by the first one's re-entrance guard.
    with trace("after"):
        client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
        )
    assert any(s.name == "cendor.trace after" for s in _spans(otel_traces))
