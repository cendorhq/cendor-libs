"""OpenTelemetry GenAI span helpers (optional). No-op if OTel isn't installed. docs/core.md §6.

Emits ``gen_ai.*`` spans following the OpenTelemetry GenAI semantic conventions, so the whole
stack speaks the standard everyone is converging on — no proprietary telemetry format.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any


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
