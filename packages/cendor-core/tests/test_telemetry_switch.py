"""The telemetry switch (DR-1/DR-6): zero telemetry code, spans still flow.

An app that configures an OpenTelemetry provider normally and uses Cendor normally must get
its calls as `gen_ai.*` spans **without writing a line of telemetry code** — Cendor emits into
the provider the app configured, and has no endpoint of its own. `CENDOR_TELEMETRY=off` turns
it all off; with OpenTelemetry absent nothing is wired at all (local-first, byte-identical).

No network: a fake client + an in-memory span exporter (fixtures in the workspace conftest).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, otel


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


@pytest.fixture(autouse=True)
def _no_switch_env(monkeypatch):
    monkeypatch.delenv(otel.TELEMETRY_ENV, raising=False)
    monkeypatch.delenv(otel.DEBUG_ENV, raising=False)


def _client(prompt=100, completion=50):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _names(exporter):
    return [s.name for s in exporter.get_finished_spans()]


# --------------------------------------------------------------------------------- the happy path


def test_zero_telemetry_code_app_emits_spans(otel_traces):
    """The acceptance test in miniature: provider + instrument() + a call ⇒ a chat span."""
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    spans = otel_traces.get_finished_spans()
    assert _names(otel_traces) == ["chat gpt-4o"]
    attrs = spans[0].attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 50
    assert attrs["gen_ai.usage.cost"]  # priced locally, on the span as a decimal string
    assert otel.auto_telemetry_state()["emitting"] is True


def test_provider_configured_after_the_first_calls_still_starts_emitting(otel_traces):
    """The predicate is re-checked per event until it succeeds — attach order never matters."""
    # `otel_traces` already installed a provider, so drop back to the proxy for the first call.
    from opentelemetry import trace
    from opentelemetry.util._once import Once

    real = trace.get_tracer_provider()
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()

    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert _names(otel_traces) == [], "no provider yet — nothing recorded"
    assert otel.auto_telemetry_state()["armed"] is True, "but the emitter is armed and waiting"

    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace.set_tracer_provider(real)
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert _names(otel_traces) == ["chat gpt-4o"], "the later provider is picked up"


def test_ingest_arms_the_emitter_too(otel_traces):
    """A managed-runtime app never calls instrument() — `otel.ingest()` is its adoption point."""
    otel.ingest(
        {
            "gen_ai.system": "az.ai.agents",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
        }
    )
    assert _names(otel_traces) == ["chat gpt-4o"]


def test_tool_calls_ride_the_same_emitter(otel_traces):
    from cendor.core import instrument_tool

    instrument(_client())  # arm via the documented adoption point

    @instrument_tool
    def get_weather(city: str) -> str:
        return "sunny"

    get_weather("Oslo")
    assert _names(otel_traces) == ["execute_tool get_weather"]


# ---------------------------------------------------------------------------------- the off switch


def test_off_kills_everything(otel_traces, monkeypatch):
    monkeypatch.setenv(otel.TELEMETRY_ENV, "off")
    assert otel.telemetry_mode() == "off"
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert _names(otel_traces) == []
    assert otel.auto_telemetry_state()["armed"] is False, "nothing is even subscribed"


def test_off_exported_late_still_silences_an_armed_emitter(otel_traces, monkeypatch):
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert len(_names(otel_traces)) == 1
    monkeypatch.setenv(otel.TELEMETRY_ENV, "off")
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert len(_names(otel_traces)) == 1, "off is honoured per event, not just at startup"


def test_an_unknown_switch_value_means_auto(otel_traces, monkeypatch):
    monkeypatch.setenv(otel.TELEMETRY_ENV, "yes-please")
    assert otel.telemetry_mode() == "auto"
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert _names(otel_traces) == ["chat gpt-4o"], "a typo must never silently disable telemetry"


def test_otel_sdk_disabled_composes_for_free(monkeypatch):
    """`OTEL_SDK_DISABLED=true` is the standard kill switch: the app's own SDK then registers no
    provider, so our predicate finds none and Cendor stays silent — nothing Cendor-specific."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert otel.provider_configured() is False


# ------------------------------------------------------------------------- local-first + no doubles


def test_without_otel_nothing_is_subscribed(no_otel):
    before = bus._subscriber_count()
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert bus._subscriber_count() == before, "byte-identical: not one extra bus subscriber"
    assert otel.provider_configured() is False
    assert otel.auto_telemetry_state()["otel"] is False


def test_manual_emitter_supersedes_the_automatic_one(otel_traces):
    """Wiring `use_span_emitter()` by hand must never double-render an event."""
    client = instrument(_client())  # arms the auto path
    off = otel.use_span_emitter()  # …which this must supersede
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert _names(otel_traces) == ["chat gpt-4o"], "exactly one span, not two"
    state = otel.auto_telemetry_state()
    assert state["manual"] == 1 and state["armed"] is False
    off()


def test_manual_first_then_instrument_is_also_single(otel_traces):
    off = otel.use_span_emitter()
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert _names(otel_traces) == ["chat gpt-4o"]
    off()


def test_disposing_the_manual_emitter_re_arms_the_automatic_one(otel_traces):
    client = instrument(_client())
    off = otel.use_span_emitter()
    off()
    instrument(_client())  # a fresh adoption re-arms
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert _names(otel_traces) == ["chat gpt-4o"]


def test_live_spans_scope_still_wins_over_the_auto_emitter(otel_traces):
    """The SDK owns spans in its run scope — the auto emitter defers, like the manual one."""
    client = instrument(_client())
    otel.enter_live_spans()
    client.chat.completions.create(model="gpt-4o", messages=[])
    otel.exit_live_spans()
    assert _names(otel_traces) == []


# ------------------------------------------------------------------------------------ cost + debug


def test_predicate_cost_is_noise(otel_traces):
    """The dormant path costs one predicate check per event, so it must stay a lookup.

    This is a **regression guard, not a benchmark** — it catches the class of mistake where the
    predicate starts doing real work per event (the TypeScript port briefly loaded a module per
    call: ~90 µs, measured). The bound is deliberately loose: a shared CI runner has shown ~3.5 µs
    for a call that is ~0.3 µs on a quiet machine, and no published number depends on this test.
    """
    n = 2000
    start = time.perf_counter()
    for _ in range(n):
        otel.provider_configured()
    per_call_us = (time.perf_counter() - start) / n * 1e6
    assert per_call_us < 20.0, f"provider_configured() took {per_call_us:.2f} µs/call"


def test_debug_env_prints_one_line(otel_traces, monkeypatch, capsys):
    monkeypatch.setenv(otel.DEBUG_ENV, "1")
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    client.chat.completions.create(model="gpt-4o", messages=[])
    err = capsys.readouterr().err
    assert "cendor telemetry:" in err
    assert err.count("emitter=attached") == 1, "one-shot — never per event"


def test_debug_env_off_is_silent(otel_traces, capsys):
    client = instrument(_client())
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert capsys.readouterr().err == "", "no ambient noise by default"
