"""Opt-in content capture (G17), thinking parse (G18), the G20 span emitter, and TTFT (G23).

The privacy assertions are the headline: capture is OFF by default, and nothing content-bearing
appears unless it is explicitly turned on. No network.
"""

import json
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, otel
from cendor.core.types import LLMCall, Money, ToolCall, Usage


@pytest.fixture(autouse=True)
def _reset():
    otel._reset_capture()
    bus._reset()
    yield
    otel._reset_capture()
    bus._reset()


# --- default OFF -------------------------------------------------------------------------------


def test_capture_off_by_default_no_content_attrs():
    assert otel.content_capture().mode == "off"
    assert (
        otel.content_attrs(
            system="you are a bot",
            input_messages=[{"role": "user", "content": "secret"}],
            output_messages=[{"role": "assistant", "parts": [{"type": "text", "content": "hi"}]}],
        )
        == {}
    )
    assert otel.tool_content_attrs(arguments={"q": "secret"}, result="answer") == {}


def test_env_var_enables_span_capture(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    assert otel.content_capture().mode == "span"
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")
    assert otel.content_capture().mode == "off"
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "span_and_event")
    assert otel.content_capture().mode == "span"


# --- ON: shape, mask, cap ----------------------------------------------------------------------


def test_content_attrs_on_are_json_strings():
    otel.capture_content()
    attrs = otel.content_attrs(
        system="you are a bot",
        input_messages=[{"role": "user", "content": "hello"}],
        output_messages=[{"role": "assistant", "parts": [{"type": "text", "content": "hi"}]}],
    )
    assert set(attrs) == {
        otel.GENAI_SYSTEM_INSTRUCTIONS,
        otel.GENAI_INPUT_MESSAGES,
        otel.GENAI_OUTPUT_MESSAGES,
    }
    assert json.loads(attrs[otel.GENAI_INPUT_MESSAGES]) == [{"role": "user", "content": "hello"}]
    assert json.loads(attrs[otel.GENAI_SYSTEM_INSTRUCTIONS])[0]["content"] == "you are a bot"


def test_mask_is_applied_before_export():
    def redact(msgs):
        return [{**m, "content": "[REDACTED]"} for m in msgs]

    otel.capture_content(mask=redact)
    attrs = otel.content_attrs(input_messages=[{"role": "user", "content": "my ssn is 123"}])
    assert "123" not in attrs[otel.GENAI_INPUT_MESSAGES]
    assert json.loads(attrs[otel.GENAI_INPUT_MESSAGES]) == [
        {"role": "user", "content": "[REDACTED]"}
    ]


def test_mask_error_fails_closed():
    def boom(_msgs):
        raise RuntimeError("bad mask")

    otel.capture_content(mask=boom)
    attrs = otel.content_attrs(input_messages=[{"role": "user", "content": "sensitive"}])
    assert "sensitive" not in attrs[otel.GENAI_INPUT_MESSAGES]
    assert "withheld" in attrs[otel.GENAI_INPUT_MESSAGES]


def test_byte_cap_truncates_with_marker():
    otel.capture_content(max_bytes=64)
    attrs = otel.content_attrs(input_messages=[{"role": "user", "content": "x" * 500}])
    assert attrs[otel.GENAI_INPUT_MESSAGES].endswith(otel.TRUNCATION_MARKER)
    assert len(attrs[otel.GENAI_INPUT_MESSAGES].encode("utf-8")) <= 64 + len(
        otel.TRUNCATION_MARKER.encode("utf-8")
    )


def test_tool_content_attrs_on():
    otel.capture_content()
    attrs = otel.tool_content_attrs(arguments={"q": "weather"}, result={"temp": 20})
    assert otel.CENDOR_TOOL_ARGUMENTS in attrs and otel.CENDOR_TOOL_RESULT in attrs
    assert "weather" in attrs[otel.CENDOR_TOOL_ARGUMENTS]


# --- G18 response_messages (per provider) ------------------------------------------------------


def _call(provider, response, **meta):
    c = LLMCall(id="x", provider=provider, model="m", messages=[])
    c.metadata["response"] = response
    c.metadata.update(meta)
    return c


def test_response_messages_openai_chat():
    resp = {"choices": [{"message": {"role": "assistant", "content": "The answer is 42."}}]}
    msgs = otel.response_messages(_call("openai", resp))
    assert msgs == [
        {"role": "assistant", "parts": [{"type": "text", "content": "The answer is 42."}]}
    ]


def test_response_messages_anthropic_thinking():
    resp = {
        "content": [
            {"type": "thinking", "thinking": "Let me reason..."},
            {"type": "text", "text": "Final answer."},
        ]
    }
    parts = otel.response_messages(_call("anthropic", resp))[0]["parts"]
    assert parts[0] == {"type": "thinking", "content": "Let me reason..."}
    assert parts[1] == {"type": "text", "content": "Final answer."}


def test_response_messages_gemini_thought():
    resp = {
        "candidates": [
            {"content": {"parts": [{"text": "hmm", "thought": True}, {"text": "answer"}]}}
        ]
    }
    parts = otel.response_messages(_call("google", resp))[0]["parts"]
    assert {"type": "thinking", "content": "hmm"} in parts
    assert {"type": "text", "content": "answer"} in parts


def test_response_messages_responses_reasoning():
    resp = {
        "output_text": "done",
        "output": [{"type": "reasoning", "summary": [{"text": "considered options"}]}],
    }
    parts = otel.response_messages(_call("openai", resp))[0]["parts"]
    assert {"type": "thinking", "content": "considered options"} in parts
    assert {"type": "text", "content": "done"} in parts


def test_response_messages_ollama_and_bedrock():
    ollama = {"message": {"content": "hi", "thinking": "quietly"}}
    parts = otel.response_messages(_call("ollama", ollama))[0]["parts"]
    assert {"type": "thinking", "content": "quietly"} in parts
    bedrock = {
        "output": {
            "message": {
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "step"}}},
                    {"text": "result"},
                ]
            }
        }
    }
    bparts = otel.response_messages(_call("bedrock", bedrock))[0]["parts"]
    assert {"type": "thinking", "content": "step"} in bparts
    assert {"type": "text", "content": "result"} in bparts


def test_response_messages_missing_returns_empty():
    assert otel.response_messages(LLMCall(id="x", provider="openai", model="m", messages=[])) == []


# --- G20 span emitter + G23 TTFT (need the OTel SDK) -------------------------------------------


def _exporter():
    """A standalone provider + in-memory exporter + tracer (never set globally — that can only
    happen once per process, so each test builds its own and passes the tracer explicitly)."""
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    inmem = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

    provider = sdk.TracerProvider()
    exp = inmem.InMemorySpanExporter()
    provider.add_span_processor(export.SimpleSpanProcessor(exp))
    return exp, provider.get_tracer("test")


def test_span_emitter_emits_chat_and_tool_spans():
    exp, tracer = _exporter()
    dispose = otel.use_span_emitter(tracer)
    try:
        call = LLMCall(
            id="1", provider="openai", model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        call.usage = Usage(10, 5)
        call.cost = Money(amount=__import__("decimal").Decimal("0.001"))
        call.latency_ms = 12.0
        bus.emit(call)
        tc = ToolCall(id="2", name="search", arguments={"q": "x"}, result="ok", latency_ms=3.0)
        bus.emit(tc)
    finally:
        dispose()
    spans = {s.name: s for s in exp.get_finished_spans()}
    assert "chat gpt-4o" in spans and "execute_tool search" in spans
    chat = spans["chat gpt-4o"]
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 10
    # content OFF by default → no content attrs on the span
    assert otel.GENAI_INPUT_MESSAGES not in chat.attributes


def test_span_emitter_includes_content_when_opted_in():
    exp, tracer = _exporter()
    otel.capture_content()
    dispose = otel.use_span_emitter(tracer)
    try:
        call = LLMCall(
            id="1", provider="openai", model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        call.metadata["response"] = {"choices": [{"message": {"content": "hello"}}]}
        call.latency_ms = 5.0
        bus.emit(call)
    finally:
        dispose()
    chat = exp.get_finished_spans()[0]
    assert json.loads(chat.attributes[otel.GENAI_INPUT_MESSAGES]) == [
        {"role": "user", "content": "hi"}
    ]
    assert "hello" in chat.attributes[otel.GENAI_OUTPUT_MESSAGES]


def test_span_emitter_defers_to_live_spans():
    exp, tracer = _exporter()
    dispose = otel.use_span_emitter(tracer)
    otel.enter_live_spans()
    try:
        bus.emit(LLMCall(id="1", provider="openai", model="gpt-4o", messages=[]))
    finally:
        otel.exit_live_spans()
        dispose()
    assert exp.get_finished_spans() == ()  # SDK live_spans owns spans; emitter stood down


def test_span_emitter_stamps_usage_estimated_only_when_set():  # G-V4-3
    exp, tracer = _exporter()
    dispose = otel.use_span_emitter(tracer)
    try:
        est = LLMCall(id="1", provider="openai", model="gpt-4o", messages=[])
        est.metadata["streamed"] = True
        est.metadata["usage_estimated"] = True  # stream reported no usage → offline estimate
        bus.emit(est)
        real = LLMCall(id="2", provider="openai", model="gpt-4o", messages=[])
        real.metadata["streamed"] = True  # real usage recovered → no est. flag
        bus.emit(real)
    finally:
        dispose()
    a, b = exp.get_finished_spans()
    assert a.attributes["cendor.usage_estimated"] == "true"  # string, only when set
    assert "cendor.usage_estimated" not in b.attributes


def test_ttft_stamped_on_stream():
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)),
    ]

    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    seen: list = []
    bus.subscribe(seen.append)
    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    stream = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    list(stream)
    call = [e for e in seen if isinstance(e, LLMCall)][0]
    assert "ttft_ms" in call.metadata and call.metadata["ttft_ms"] >= 0.0
