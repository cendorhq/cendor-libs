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
    from .ambient import apply_ambient
    from .instrument import current_trace_id
    from .types import LLMCall, Usage

    _arm_auto_telemetry()  # a managed-runtime app never calls instrument() — this is its adoption
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
        # GLR-8: stamp the ambient trace id at construction so an ingested call joins its run.
        trace_id=current_trace_id(),
        ts=datetime.now(UTC),
    )
    call.metadata["source"] = "otel"
    apply_ambient(call)
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
# The telemetry switch (DR-1 / DR-6) — "it just flows".
#
# Cendor emits into the OpenTelemetry provider **your app configured**; it has no endpoint, no
# exporter and no collector of its own. So when OTel is installed AND a real (non-default) global
# provider exists, emitting is the useful default — the same posture every OTel instrumentation
# library takes. ``CENDOR_TELEMETRY=off`` turns all of it off, process-wide, with no code change.
#
# Nothing here is identity: the app name stays the OTel resource's ``service.name``
# (``OTEL_SERVICE_NAME``), and there is no Cendor identity env var.
# =================================================================================================

#: The one switch. ``off`` disables every Cendor-side emitter; unset (or ``auto``) means
#: "emit when a provider is configured".
TELEMETRY_ENV = "CENDOR_TELEMETRY"
#: Set to ``1`` for a one-shot stderr line describing what was detected and wired.
DEBUG_ENV = "CENDOR_DEBUG_TELEMETRY"

_debug_said: set[str] = set()


def telemetry_mode() -> Literal["auto", "off"]:
    """The effective telemetry mode from ``CENDOR_TELEMETRY``: ``"auto"`` (default) or ``"off"``.

    ``auto`` means *emit when the app has configured an OpenTelemetry provider* (see
    :func:`provider_configured`). ``off`` disables every Cendor-side emitter — the span emitter, the
    spend tap, and the audit mirror's auto-attach — without touching your code. An unrecognised
    value
    is treated as ``auto`` (noted once under ``CENDOR_DEBUG_TELEMETRY=1``), because a typo must
    never
    silently disable telemetry.

    Example:
        >>> from cendor.core import otel
        >>> otel.telemetry_mode()          # 'auto' unless CENDOR_TELEMETRY=off
        'auto'
    """
    raw = os.environ.get(TELEMETRY_ENV, "").strip().lower()
    if raw == "off":
        return "off"
    if raw not in ("", "auto"):
        _debug(f"CENDOR_TELEMETRY={raw!r} is not 'auto' or 'off' — treating it as 'auto'")
    return "auto"


def provider_configured() -> bool:
    """True when the app has registered a real (non-default) global OpenTelemetry tracer provider.

    This is the honest signal that *somebody is listening*: it is False before the app's one-time
    OTel setup (the API hands out a ``ProxyTracerProvider``) and True after it. It never inspects
    exporters or endpoints — Cendor does not care where your spans go. False when OpenTelemetry is
    not installed at all.

    Example:
        >>> from cendor.core import otel
        >>> otel.provider_configured()     # False until your app configures OTel
        False
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace import ProxyTracerProvider
    except ImportError:
        return False
    return not isinstance(trace.get_tracer_provider(), ProxyTracerProvider)


def _debug(message: str) -> None:
    """One-shot stderr note, only under ``CENDOR_DEBUG_TELEMETRY=1``. Never warns by default: the
    silent no-op is load-bearing for local-first (an offline app must not be nagged)."""
    if os.environ.get(DEBUG_ENV, "").strip() not in ("1", "true", "TRUE", "yes"):
        return
    if message in _debug_said:
        return
    _debug_said.add(message)
    import sys

    print(f"cendor telemetry: {message}", file=sys.stderr)


def _otel_importable() -> bool:
    try:
        import opentelemetry.trace  # noqa: F401
    except ImportError:
        return False
    return True


# =================================================================================================
# G20 — bus→span emitter. Turns LLMCall/ToolCall bus events into semconv spans, so a libs-only app
# (no SDK) lights up a trace-based monitor. Honors content capture. Attached automatically under the
# switch above (and explicitly by use_span_emitter, which always wins).
# =================================================================================================

#: Nonzero while an SDK ``live_spans`` context is open — the emitter defers to it (no double spans).
_live_span_depth: ContextVar[int] = ContextVar("cendor_live_span_depth", default=0)


def enter_live_spans() -> None:
    """Called by the SDK when a ``live_spans`` context opens, so the G20 emitter stands down."""
    _live_span_depth.set(_live_span_depth.get() + 1)


def exit_live_spans() -> None:
    """Called by the SDK when a ``live_spans`` context closes."""
    _live_span_depth.set(max(0, _live_span_depth.get() - 1))


def live_spans_active() -> bool:
    """True while an SDK ``live_spans`` scope is open in this context.

    The SDK reads it to decide whether to open its **automatic** run scope: an explicit
    ``live_spans()`` the user opened always wins, so a run is never wrapped twice.

    Example:
        >>> from cendor.core import otel
        >>> otel.live_spans_active()
        False
    """
    return _live_span_depth.get() > 0


def _live_spans_active() -> bool:
    return live_spans_active()


def use_span_emitter(tracer: Any = None) -> Callable[[], None]:
    """Emit a ``chat``/``execute_tool`` semconv span per ``LLMCall``/``ToolCall`` bus event.

    **You usually do not need to call this.** Under ``CENDOR_TELEMETRY=auto`` (the default) the same
    emitter attaches itself as soon as you use ``instrument()`` and your app has an OpenTelemetry
    provider configured. Call it explicitly to pass your own ``tracer``, or to emit regardless of
    the
    switch. **A manual call always wins:** it detaches the automatic subscription, so events are
    never
    rendered twice.

    Honors content capture (G17). When an SDK ``live_spans`` context is active it defers to it (no
    double spans); it is otherwise mutually exclusive with the SDK's ``span_tree``/``live_spans`` —
    don't wire both for the same run.

    Returns a disposer that unsubscribes the emitter (and re-arms the automatic path). No-op if
    OpenTelemetry isn't installed and no tracer was passed.
    """
    from . import bus

    # An explicitly passed tracer wins and needs no OpenTelemetry import: the caller already has one
    # (a custom tracer, or a recording double). Only the default path needs the package — and
    # returns
    # a no-op disposer without it. (Before W0.5 the ImportError check ran first, so passing a tracer
    # into an env without OTel installed silently subscribed nothing — the TS port never had that
    # asymmetry: `tracer ?? loadRichTracer()`.)
    tr = tracer
    if tr is None:
        try:
            from opentelemetry import trace
        except ImportError:
            return lambda: None
        tr = trace.get_tracer("cendor.core")

    def on_event(ev: Any) -> None:
        _render_bus_event(tr, ev)

    global _manual_emitters
    _manual_emitters += 1
    _detach_auto_emitter()  # manual wins — exactly one emitter, ever
    bus.subscribe(on_event)
    _debug("span emitter attached (manual)")

    def dispose() -> None:
        global _manual_emitters
        bus.unsubscribe(on_event)
        _manual_emitters = max(0, _manual_emitters - 1)

    return dispose


def _render_bus_event(tr: Any, ev: Any) -> None:
    """Render one bus event as a span (the shared body of the manual + automatic emitters)."""
    from .types import LLMCall, ToolCall

    if _live_spans_active():
        return
    if isinstance(ev, LLMCall):
        _emit_llm_span(tr, ev)
    elif isinstance(ev, ToolCall):
        _emit_tool_span(tr, ev)
    else:
        _emit_governance_span(tr, ev)


# =================================================================================================
# Option C (DR-2c) — governance ENFORCEMENT as ordinary telemetry.
#
# A telemetry user wants to see the decisions their stack made: a budget that blocked a call, a
# guardrail that tripped. Until now the only wire path for those was the *audit mirror* — so
# seeing them meant adopting the evidence library. Option C renders them as plain monitoring
# spans instead:
#
#   governance.budget_event · governance.guardrail_decision   (scope cendor.core / cendor.sdk)
#
# Deliberately **no ``audit.*`` vocabulary and no AuditLog involved** (rule 6): these are
# operational signals, and "audit" keeps meaning the hash-chained evidence file. When a real audit
# mirror IS on the wire, the ops renderings stand down (:func:`governance_mirror_active`), so
# nothing renders twice.
#
# Content: metadata only. The events' ``reason`` strings are NOT emitted — a guardrail's reason
# comes from the rule, and for ``rules.llm_judge`` from a judge *model* (free text that can
# paraphrase the payload; ``rules.url_*`` embeds the matched host), so it can carry input-derived
# text. The audit chain — an artifact the user explicitly declared — keeps carrying it; these
# default-on spans do not.
# =================================================================================================

#: How many live audit mirrors are on the wire (refcounted by ``acttrace``). While > 0, governance
#: enforcement events are already arriving as ``audit.*`` spans, so the ops renderings stand down.
_gov_mirrors = 0


def governance_mirrored(on: bool) -> None:
    """Tell core that an audit mirror is (or is no longer) putting governance on the wire.

    Called by ``cendor-acttrace`` when an ``AuditLog`` attaches or detaches a mirror that emits
    OpenTelemetry spans. Refcounted, so several logs compose. While the count is above zero, the
    Option C ``governance.*`` spans stand down — the mirror is richer (chained, hashed,
    sequenced) and must win, and an event must never render twice.
    """
    global _gov_mirrors
    _gov_mirrors = max(0, _gov_mirrors + (1 if on else -1))


def governance_mirror_active() -> bool:
    """True while at least one audit mirror is putting governance on the wire."""
    return _gov_mirrors > 0


def _reset_governance_mirrors() -> None:
    """Test helper: forget the mirror refcount."""
    global _gov_mirrors
    _gov_mirrors = 0


def _gov_attrs(ev: Any) -> tuple[str, dict[str, Any]] | None:
    """Map an enforcement event to ``(span name, cendor.gov.* attrs)``, or None if it isn't one.

    Duck-typed exactly like ``acttrace``'s chaining (core imports no tool — rule 2). Fields are the
    factual ones only: what acted, at which stage, with which numbers.
    """
    # tokenguard BudgetEvent
    if hasattr(ev, "action") and hasattr(ev, "projected_usd") and hasattr(ev, "cap_usd"):
        attrs: dict[str, Any] = {
            "cendor.gov.type": "budget_event",
            "cendor.gov.action": str(getattr(ev, "action", "") or ""),
            "cendor.gov.budget": getattr(ev, "name", None),
            "cendor.gov.scope": getattr(ev, "scope", None),
            "cendor.gov.model": getattr(ev, "model", None) or None,
            "cendor.gov.to_model": getattr(ev, "to_model", None),
            "cendor.gov.projected_usd": _str_or_none(getattr(ev, "projected_usd", None)),
            "cendor.gov.cap_usd": _str_or_none(getattr(ev, "cap_usd", None)),
            "cendor.gov.projected_tokens": getattr(ev, "projected_tokens", None),
            "cendor.gov.cap_tokens": getattr(ev, "cap_tokens", None),
        }
        return "governance.budget_event", attrs
    # guardrails GuardrailDecision
    if hasattr(ev, "guardrail") and hasattr(ev, "stage") and hasattr(ev, "action"):
        return "governance.guardrail_decision", {
            "cendor.gov.type": "guardrail_decision",
            "cendor.gov.guardrail": str(getattr(ev, "guardrail", "") or ""),
            "cendor.gov.stage": str(getattr(ev, "stage", "") or ""),
            "cendor.gov.action": str(getattr(ev, "action", "") or ""),
            "cendor.gov.agent": getattr(ev, "agent", "") or None,
            "cendor.gov.tool": getattr(ev, "tool", "") or None,
        }
    return None


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _emit_governance_span(tr: Any, ev: Any) -> None:
    """Render an enforcement event as a ``governance.*`` span (Option C). Zero-duration: a
    decision is a point in time, not an operation with a span of work."""
    if governance_mirror_active():
        return  # the audit mirror is on the wire — it wins (no double render)
    mapped = _gov_attrs(ev)
    if mapped is None:
        return
    name, attrs = mapped
    now = time.time_ns()
    span = tr.start_span(name, start_time=now)
    try:
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)
        trace_id = getattr(ev, "trace_id", "") or ""
        if trace_id:
            # The monitor joins this to the run row exactly like a chat span's cendor.trace_id.
            span.set_attribute("cendor.trace_id", trace_id)
    finally:
        span.end(end_time=now)


# --------------------------------------------------------------- automatic attach (DR-1 = "auto")
# One subscription, made the first time the app adopts a capture path (``instrument()`` /
# ``ingest()``) and only when OpenTelemetry is importable. It stays dormant — re-checking the cheap
# provider predicate per event (~300 ns, measured) — until the app's provider appears, then latches
# and renders. So attach order never matters, a provider configured after the first call is still
# caught, and an app that never configures OTel pays a predicate check and nothing else.
_auto_emitter: Any = None  # the subscribed callable, or None
_auto_ready = False  # True once a real provider was seen (the latch)
_manual_emitters = 0  # >0 ⇒ the user wired their own; the auto path stands down


def _auto_on_event(ev: Any) -> None:
    global _auto_ready
    if _manual_emitters:
        return
    if telemetry_mode() == "off":
        return  # read per event: `off` takes effect even if exported late
    if not _auto_ready:
        if not provider_configured():
            return
        _auto_ready = True
        _debug("mode=auto, provider=detected, emitter=attached")
    from opentelemetry import trace

    _render_bus_event(trace.get_tracer("cendor.core"), ev)


def _arm_auto_telemetry() -> None:
    """Called from the capture entry points (``instrument()``, :func:`ingest`). Idempotent +
    cheap."""
    global _auto_emitter
    if _auto_emitter is not None or _manual_emitters:
        return
    if telemetry_mode() == "off":
        return
    if not _otel_importable():
        # Local-first rail: with OpenTelemetry absent nothing is subscribed at all — the bus keeps
        # exactly the subscribers it had, and behaviour is byte-identical to a pre-switch release.
        return
    from . import bus

    _auto_emitter = _auto_on_event
    bus.subscribe(_auto_on_event)
    _debug("armed (mode=auto); waiting for a provider" if not provider_configured() else "armed")


def _detach_auto_emitter() -> None:
    global _auto_emitter
    if _auto_emitter is None:
        return
    from . import bus

    bus.unsubscribe(_auto_emitter)
    _auto_emitter = None


def _reset_auto_telemetry() -> None:
    """Test helper: forget the automatic subscription + its latch."""
    global _auto_ready, _manual_emitters
    _detach_auto_emitter()
    _auto_ready = False
    _manual_emitters = 0
    _debug_said.clear()


def auto_telemetry_state() -> dict[str, Any]:
    """What the automatic path currently thinks — for diagnostics (``doctor``) and tests.

    Keys: ``mode`` (``auto``/``off``), ``otel`` (importable), ``provider`` (configured),
    ``armed`` (a dormant-or-live auto subscription exists), ``emitting`` (the latch fired),
    ``manual`` (how many explicit ``use_span_emitter`` attachments are live).
    """
    return {
        "mode": telemetry_mode(),
        "otel": _otel_importable(),
        "provider": provider_configured(),
        "armed": _auto_emitter is not None,
        "emitting": _auto_ready and not _manual_emitters,
        "manual": _manual_emitters,
    }


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
        if (call.metadata or {}).get("usage_estimated"):
            # Truth = the product: mark streamed token counts recovered by offline estimate (not the
            # provider's billed figure) so a monitor can render them as "est." rather than exact.
            span.set_attribute("cendor.usage_estimated", "true")
        if (call.metadata or {}).get("replayed"):
            span.set_attribute("cendor.replayed", True)
        if call.trace_id:
            span.set_attribute("cendor.trace_id", call.trace_id)
        # GLR-10 (D2=YES): surface an ambient-stamped agent (a libs-only app's own
        # add_ambient_provider, or the LangChain handler's node/chain name — GLR-11a) on semconv
        # semconv attribute, so a trace-based monitor shows it. Core invents nothing — only what was
        # stamped.
        agent = (call.metadata or {}).get("agent")
        if isinstance(agent, str) and agent:
            span.set_attribute("gen_ai.agent.name", agent)
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
