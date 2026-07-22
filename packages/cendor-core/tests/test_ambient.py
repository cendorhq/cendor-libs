"""Ambient metadata seam (GLR-1) — the core-owned pre-emit capture point. Contract: never-raise,
never-overwrite, registration order, zero-provider byte-identical no-op (the libs-standalone
contract). Plus the GLR-8 ingest trace-id stamp and GLR-10 span-emitter agent mapping."""

from types import SimpleNamespace

import pytest
from cendor.core import (
    LLMCall,
    add_ambient_provider,
    bus,
    instrument,
    otel,
    remove_ambient_provider,
    trace,
)
from cendor.core.ambient import _reset_ambient, apply_ambient


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    _reset_ambient()
    yield
    bus._reset()
    _reset_ambient()


def _call():
    return LLMCall(id="x", provider="openai", model="gpt-4o", messages=[])


def test_zero_providers_is_byte_identical_noop():
    call = _call()
    call.metadata["request_kwargs"] = {"model": "gpt-4o"}
    before = dict(call.metadata)
    apply_ambient(call)
    assert call.metadata == before


def test_merges_and_never_overwrites():
    add_ambient_provider(lambda e: {"agent": "from-provider", "extra": 1})
    call = _call()
    call.metadata["agent"] = "explicit"
    apply_ambient(call)
    assert call.metadata["agent"] == "explicit"  # explicit value wins
    assert call.metadata["extra"] == 1


def test_registration_order_first_wins():
    add_ambient_provider(lambda e: {"k": "first"})
    add_ambient_provider(lambda e: {"k": "second"})
    call = _call()
    apply_ambient(call)
    assert call.metadata["k"] == "first"


def test_never_raises_and_later_provider_still_runs():
    def boom(_e):
        raise RuntimeError("boom")

    add_ambient_provider(boom)
    add_ambient_provider(lambda e: {"survived": True})
    call = _call()
    apply_ambient(call)  # does not raise
    assert call.metadata["survived"] is True


def test_remove_ambient_provider():
    p = add_ambient_provider(lambda e: {"agent": "x"})
    remove_ambient_provider(p)
    call = _call()
    apply_ambient(call)
    assert "agent" not in call.metadata


def test_stamps_through_instrument():
    add_ambient_provider(lambda e: {"agent": "writer"})
    calls: list = []
    bus.subscribe(lambda e: calls.append(e) if isinstance(e, LLMCall) else None)

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    with trace("run-1"):
        client.chat.completions.create(model="gpt-4o", messages=[])
    assert len(calls) == 1
    assert calls[0].metadata["agent"] == "writer"
    assert calls[0].trace_id == "run-1"


def test_ingest_stamps_trace_id_and_runs_providers():
    add_ambient_provider(lambda e: {"agent": "ingested"})
    with trace("run-42"):
        call = otel.ingest(
            {
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.system": "openai",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 3,
            }
        )
    assert call.trace_id == "run-42"  # GLR-8
    assert call.metadata["agent"] == "ingested"


def test_span_emitter_maps_agent_to_gen_ai_agent_name():
    # GLR-10: a libs-only app self-identifies an agent via metadata.agent -> gen_ai.agent.name.
    pytest.importorskip("opentelemetry")  # use_span_emitter is a no-op without the [otel] extra
    spans: list = []

    class FakeSpan:
        def __init__(self):
            self.attrs: dict = {}

        def set_attribute(self, k, v):
            self.attrs[k] = v

        def end(self, end_time=None):
            pass

    class FakeTracer:
        def start_span(self, name, start_time=None):
            s = FakeSpan()
            spans.append(s)
            return s

    dispose = otel.use_span_emitter(FakeTracer())
    named = _call()
    named.metadata["agent"] = "reviewer"
    bus.emit(named)
    anon = _call()
    bus.emit(anon)
    dispose()
    assert spans[0].attrs.get("gen_ai.agent.name") == "reviewer"
    assert "gen_ai.agent.name" not in spans[1].attrs  # core invents nothing
