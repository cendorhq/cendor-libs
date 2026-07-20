"""OpenTelemetry GenAI span helpers (optional). No-op if OTel isn't installed. docs/core.md §6.

Emits ``gen_ai.*`` spans following the OpenTelemetry GenAI semantic conventions, so the whole
stack speaks the standard everyone is converging on — no proprietary telemetry format.

Content capture (prompts/responses/thinking/tool values) is **opt-in and OFF by default** — call
:func:`capture_content` or set ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT``. It rides the
semconv's own content span attributes and NEVER enters the acttrace evidence chain. See
docs/observability.md.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal


@contextmanager
def span(model: str, *, provider: str | None = None, **attributes: Any) -> Iterator[Any]:
    """Emit a ``gen_ai`` span around a call. Yields the span, or ``None`` if OTel is absent.

    Args:
        model: Model id, recorded as ``gen_ai.request.model``.
        provider: Optional system name, recorded as ``gen_ai.system``.
        **attributes: Extra span attributes to set verbatim.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return

    tracer = trace.get_tracer("cendor.core")
    with tracer.start_as_current_span(f"chat {model}") as current:
        current.set_attribute("gen_ai.request.model", model)
        if provider is not None:
            current.set_attribute("gen_ai.system", provider)
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield current


def ingest(attributes: dict, *, messages: list[dict] | None = None, emit: bool = True) -> Any:
    """Turn OpenTelemetry GenAI (``gen_ai.*``) span attributes into a normalized ``LLMCall``.

    This is the **managed-runtime capture path** (docs/architecture.md §4): when a server-side
    runtime (Foundry Agent Service, OpenAI Assistants) owns the loop and only emits ``gen_ai.*``
    spans, feed those attributes here and the call joins the same event bus — so ``tokenguard``
    and ``acttrace`` consume it exactly as if it had been instrumented locally. No OTel dependency.

    Reads ``gen_ai.request.model`` (or ``response.model``), ``gen_ai.system``, and the usage
    attributes; prices it via ``prices``; emits on the bus unless ``emit=False``. Returns the call.
    """
    from . import bus, prices
    from .types import LLMCall, Usage

    model = attributes.get("gen_ai.request.model") or attributes.get("gen_ai.response.model") or ""
    provider = attributes.get("gen_ai.system", "")
    inp = attributes.get("gen_ai.usage.input_tokens", attributes.get("gen_ai.usage.prompt_tokens"))
    out = (
        attributes.get(
            "gen_ai.usage.output_tokens", attributes.get("gen_ai.usage.completion_tokens")
        )
        or 0
    )
    # Cached / reasoning breakdowns, if the runtime reports them — so managed-runtime capture keeps
    # the same detail as a local call (accepts a couple of common attribute spellings).
    cached = (
        attributes.get(
            "gen_ai.usage.cached_tokens", attributes.get("gen_ai.usage.cache_read_input_tokens")
        )
        or 0
    )
    reasoning = attributes.get("gen_ai.usage.reasoning_tokens") or 0
    usage = Usage(int(inp), int(out), int(cached), int(reasoning)) if inp is not None else None
    call = LLMCall(
        id=uuid.uuid4().hex,
        provider=str(provider),
        model=str(model),
        messages=messages or [],
        usage=usage,
        ts=datetime.now(UTC),
    )
    call.metadata["source"] = "otel"
    if usage is not None:
        try:
            call.cost = prices.estimate(
                call.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens
            )
        except KeyError:
            call.cost = None
    if emit:
        bus.emit(call)
    return call


# =================================================================================================
# Content capture (G17) — opt-in, OFF by default. Prompts/responses/thinking/tool values ride the
# OpenTelemetry GenAI content span attributes. Nothing here touches the acttrace chain (rule 6).
# =================================================================================================

#: Semconv content attribute keys (JSON-string values).
GENAI_INPUT_MESSAGES = "gen_ai.input.messages"
GENAI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GENAI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
#: Cendor tool-content lane on ``execute_tool`` spans (semconv has none for arg/result *values*).
CENDOR_TOOL_ARGUMENTS = "cendor.tool.arguments"
CENDOR_TOOL_RESULT = "cendor.tool.result"

#: The standard env var (per the GenAI semconv). Any enabling value turns span capture on.
_CAPTURE_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
_ENABLING = frozenset({"true", "span_only", "span_and_event"})
#: Appended to a content string when the per-attribute byte cap truncates it.
TRUNCATION_MARKER = "…[cendor: content truncated]"


@dataclass
class CaptureConfig:
    """The content-capture configuration. ``mode='off'`` (the default) exports no content."""

    mode: Literal["off", "span"] = "off"
    mask: Callable[[list[dict]], list[dict]] | None = None
    max_bytes: int = 8192


_capture = CaptureConfig()


def capture_content(
    mode: Literal["off", "span"] = "span",
    *,
    mask: Callable[[list[dict]], list[dict]] | None = None,
    max_bytes: int = 8192,
) -> None:
    """Enable opt-in content capture on ``gen_ai.*`` span attributes (OFF by default).

    Content — system prompts, user/assistant messages, thinking text, tool arg/result values —
    then rides the standard semconv span attributes and lands wherever your OTLP goes (Cendor
    Monitor, Langfuse, Braintrust — same wire). It is **never** written to the acttrace evidence
    chain or its mirror (rule 6). Pair with a ``mask`` to scrub before export and ``max_bytes`` to
    cap each attribute (a truncation marker is appended when hit).

    Args:
        mode: ``"span"`` to capture onto span attributes, ``"off"`` to disable.
        mask: Optional callable run over each message list before export
            (``messages -> messages``); a docs example wires a guardrails detector as the mask. If
            it raises, content is withheld (fail-closed), not exported unmasked.
        max_bytes: Per-attribute byte cap (default 8 KiB).

    Example:
        >>> from cendor.core import otel
        >>> otel.capture_content(max_bytes=4096)  # opt in; then your spans carry masked content
    """
    global _capture
    if mode not in ("off", "span"):
        raise ValueError(f"mode must be 'off' or 'span', got {mode!r}")
    _capture = CaptureConfig(mode=mode, mask=mask, max_bytes=max_bytes)


def content_capture() -> CaptureConfig:
    """The effective capture config: explicit code config wins; else the standard env var may
    enable span capture. Off unless one of them turns it on."""
    if _capture.mode != "off":
        return _capture
    if os.environ.get(_CAPTURE_ENV, "").strip().lower() in _ENABLING:
        return CaptureConfig(mode="span", mask=_capture.mask, max_bytes=_capture.max_bytes)
    return _capture


def _reset_capture() -> None:
    """Test helper: restore the default (off) capture config."""
    global _capture
    _capture = CaptureConfig()


def _encode(cfg: CaptureConfig, messages: list[dict]) -> str | None:
    """Mask, JSON-encode, and byte-cap a message list. Returns None for empty input; a fail-closed
    marker string if the user mask raises."""
    if not messages:
        return None
    msgs: Any = messages
    if cfg.mask is not None:
        try:
            safe = [dict(m) if isinstance(m, dict) else m for m in messages]
            masked = cfg.mask(safe)
            msgs = masked if masked is not None else messages
        except Exception:  # noqa: BLE001 — a broken mask must never leak unmasked content
            return json.dumps("[cendor: mask raised; content withheld]")
    try:
        text = json.dumps(msgs, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — content must never break the app
        text = json.dumps(str(msgs))
    raw = text.encode("utf-8")
    if len(raw) > cfg.max_bytes:
        text = raw[: cfg.max_bytes].decode("utf-8", "ignore") + TRUNCATION_MARKER
    return text


def content_attrs(
    *,
    system: Any = None,
    input_messages: list[dict] | None = None,
    output_messages: list[dict] | None = None,
) -> dict[str, str]:
    """Build the ``gen_ai.*`` content span attributes for the active capture config, or ``{}`` when
    capture is off. ``system`` may be a string (from an agent's instructions) or a message list."""
    cfg = content_capture()
    if cfg.mode == "off":
        return {}
    out: dict[str, str] = {}
    if system:
        sys_msgs = (
            system if isinstance(system, list) else [{"role": "system", "content": str(system)}]
        )
        v = _encode(cfg, sys_msgs)
        if v is not None:
            out[GENAI_SYSTEM_INSTRUCTIONS] = v
    if input_messages:
        v = _encode(cfg, input_messages)
        if v is not None:
            out[GENAI_INPUT_MESSAGES] = v
    if output_messages:
        v = _encode(cfg, output_messages)
        if v is not None:
            out[GENAI_OUTPUT_MESSAGES] = v
    return out


def tool_content_attrs(arguments: Any = None, result: Any = None) -> dict[str, str]:
    """Content span attributes for an ``execute_tool`` span (arg/result values), or ``{}`` when
    capture is off. Values are wrapped as tool messages so the same mask/cap pipeline applies."""
    cfg = content_capture()
    if cfg.mode == "off":
        return {}
    out: dict[str, str] = {}
    if arguments is not None:
        v = _encode(cfg, [{"role": "tool", "content": arguments}])
        if v is not None:
            out[CENDOR_TOOL_ARGUMENTS] = v
    if result is not None:
        v = _encode(cfg, [{"role": "tool", "content": result}])
        if v is not None:
            out[CENDOR_TOOL_RESULT] = v
    return out


def _content_get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _flatten_text(content: Any) -> str:
    """Assistant message ``content`` may be a string or a list of parts — join the text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            t = _content_get(p, "text")
            if t:
                parts.append(str(t))
        return "".join(parts)
    return "" if content is None else str(content)


def _assistant_msg(texts: list[str], thinks: list[str]) -> list[dict]:
    parts: list[dict] = []
    for t in thinks:
        if t:
            parts.append({"type": "thinking", "content": t})
    for t in texts:
        if t:
            parts.append({"type": "text", "content": t})
    return [{"role": "assistant", "parts": parts}] if parts else []


def response_messages(call: Any) -> list[dict]:
    """Best-effort assistant output messages (text + thinking parts, G18) parsed from a completed
    call's raw provider response, per provider. Content only — returns ``[]`` if unavailable or the
    response isn't parseable. Thinking is surfaced as a ``{"type": "thinking"}`` part (the semconv
    part shape for reasoning is still Development)."""
    meta = getattr(call, "metadata", None) or {}
    resp = meta.get("response")
    if resp is None:
        return []
    provider = getattr(call, "provider", "")
    try:
        if meta.get("streamed") and isinstance(resp, list):
            from .instrument import _stream_text  # lazy; avoids an import cycle

            text = "".join(_stream_text(ch, _internal_provider(call, provider)) for ch in resp)
            return _assistant_msg([text], [])
        texts: list[str] = []
        thinks: list[str] = []
        if provider == "anthropic":
            for b in _content_get(resp, "content") or []:
                bt = _content_get(b, "type")
                if bt == "text":
                    texts.append(str(_content_get(b, "text", "") or ""))
                elif bt == "thinking":
                    thinks.append(str(_content_get(b, "thinking", "") or ""))
        elif provider == "google":
            for c in (_content_get(resp, "candidates") or [])[:1]:
                for p in _content_get(_content_get(c, "content"), "parts") or []:
                    txt = _content_get(p, "text")
                    if not txt:
                        continue
                    (thinks if _content_get(p, "thought") else texts).append(str(txt))
        elif provider == "ollama":
            m = _content_get(resp, "message")
            c = _content_get(m, "content")
            if c:
                texts.append(str(c))
            th = _content_get(m, "thinking")
            if th:
                thinks.append(str(th))
        elif provider == "bedrock":
            content = (
                _content_get(_content_get(_content_get(resp, "output"), "message"), "content") or []
            )
            for b in content:
                if _content_get(b, "text"):
                    texts.append(str(_content_get(b, "text")))
                rc = _content_get(b, "reasoningContent")
                if rc:
                    rt = _content_get(_content_get(rc, "reasoningText"), "text")
                    if rt:
                        thinks.append(str(rt))
        else:  # openai chat completions, openai responses, huggingface
            choices = _content_get(resp, "choices")
            if choices:
                msg = _content_get(choices[0], "message")
                texts.append(_flatten_text(_content_get(msg, "content")))
                rc = _content_get(msg, "reasoning_content")  # deepseek-style
                if rc:
                    thinks.append(str(rc))
            else:  # responses API
                ot = _content_get(resp, "output_text")
                if ot:
                    texts.append(str(ot))
                for item in _content_get(resp, "output") or []:
                    itype = _content_get(item, "type")
                    if itype == "reasoning":
                        for s in _content_get(item, "summary") or []:
                            st = _content_get(s, "text")
                            if st:
                                thinks.append(str(st))
                    elif itype == "message" and not ot:
                        for part in _content_get(item, "content") or []:
                            pt = _content_get(part, "text")
                            if pt:
                                texts.append(str(pt))
        return _assistant_msg(texts, thinks)
    except Exception:  # noqa: BLE001 — content extraction is best-effort, never fatal
        return []


def _internal_provider(call: Any, public: str) -> str:
    """Map a public provider back to the internal spelling ``_stream_text`` expects for OpenAI."""
    if public == "openai":
        # A Responses-API stream carries typed events; a Chat-Completions stream carries choices.
        meta = getattr(call, "metadata", None) or {}
        resp = meta.get("response")
        if isinstance(resp, list) and resp and _content_get(resp[0], "type"):
            return "openai_responses"
    return public


# =================================================================================================
# G20 — bus→span emitter. Opt-in subscriber that turns LLMCall/ToolCall bus events into semconv
# spans, so a libs-only app (no SDK) lights up a trace-based monitor. Honors content capture.
# =================================================================================================

#: Nonzero while an SDK ``live_spans`` context is open — the emitter defers to it (no double spans).
_live_span_depth: ContextVar[int] = ContextVar("cendor_live_span_depth", default=0)


def enter_live_spans() -> None:
    """Called by the SDK when a ``live_spans`` context opens, so the G20 emitter stands down."""
    _live_span_depth.set(_live_span_depth.get() + 1)


def exit_live_spans() -> None:
    """Called by the SDK when a ``live_spans`` context closes."""
    _live_span_depth.set(max(0, _live_span_depth.get() - 1))


def _live_spans_active() -> bool:
    return _live_span_depth.get() > 0


def use_span_emitter(tracer: Any = None) -> Callable[[], None]:
    """Opt-in: emit a ``chat``/``execute_tool`` semconv span per ``LLMCall``/``ToolCall`` bus event.

    A libs-only app (using ``instrument()`` but not the SDK) can wire this once to light up any
    trace-based monitor without writing manual spans. Honors content capture (G17). When an SDK
    ``live_spans`` context is active it defers to it (no double spans); it is otherwise mutually
    exclusive with the SDK's ``span_tree``/``live_spans`` — don't wire both for the same run.

    Returns a disposer that unsubscribes the emitter. No-op if OpenTelemetry isn't installed.
    """
    from . import bus
    from .types import LLMCall, ToolCall

    try:
        from opentelemetry import trace
    except ImportError:
        return lambda: None
    tr = tracer or trace.get_tracer("cendor.core")

    def on_event(ev: Any) -> None:
        if _live_spans_active():
            return
        if isinstance(ev, LLMCall):
            _emit_llm_span(tr, ev)
        elif isinstance(ev, ToolCall):
            _emit_tool_span(tr, ev)

    bus.subscribe(on_event)
    return lambda: bus.unsubscribe(on_event)


def _emit_llm_span(tr: Any, call: Any) -> None:
    latency = call.latency_ms or 0.0
    end = time.time_ns()
    span = tr.start_span(f"chat {call.model}", start_time=end - int(latency * 1_000_000))
    try:
        span.set_attribute("gen_ai.operation.name", "chat")
        if call.provider:
            span.set_attribute("gen_ai.system", call.provider)
        span.set_attribute("gen_ai.request.model", call.model)
        u = call.usage
        if u is not None:
            span.set_attribute("gen_ai.usage.input_tokens", u.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", u.output_tokens)
            if u.reasoning_tokens:
                span.set_attribute("gen_ai.usage.reasoning_tokens", u.reasoning_tokens)
        if call.cost is not None:
            span.set_attribute("gen_ai.usage.cost", str(call.cost.amount))
        if call.latency_ms is not None:
            span.set_attribute("cendor.latency_ms", call.latency_ms)
        ttft = (call.metadata or {}).get("ttft_ms")
        if ttft is not None:
            span.set_attribute("cendor.ttft_ms", ttft)
        if (call.metadata or {}).get("streamed"):
            span.set_attribute("cendor.streamed", True)
        if (call.metadata or {}).get("replayed"):
            span.set_attribute("cendor.replayed", True)
        if call.trace_id:
            span.set_attribute("cendor.trace_id", call.trace_id)
        for k, v in content_attrs(
            input_messages=call.messages, output_messages=response_messages(call)
        ).items():
            span.set_attribute(k, v)
    finally:
        span.end(end_time=end)


def _emit_tool_span(tr: Any, tc: Any) -> None:
    latency = tc.latency_ms or 0.0
    end = time.time_ns()
    span = tr.start_span(f"execute_tool {tc.name}", start_time=end - int(latency * 1_000_000))
    try:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", tc.name)
        if tc.latency_ms is not None:
            span.set_attribute("cendor.latency_ms", tc.latency_ms)
        if tc.trace_id:
            span.set_attribute("cendor.trace_id", tc.trace_id)
        for k, v in tool_content_attrs(arguments=tc.arguments, result=tc.result).items():
            span.set_attribute(k, v)
    finally:
        span.end(end_time=end)
