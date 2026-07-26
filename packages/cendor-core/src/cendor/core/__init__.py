"""cendor.core — the shared foundation. Keep this public surface small and stable."""

from __future__ import annotations

from . import bus, otel, prices, protocols, tokens
from .ambient import add_ambient_provider, ambient_attrs, remove_ambient_provider
from .instrument import (
    MISS,
    Reroute,
    add_interceptor,
    add_stream_observer,
    current_trace_id,
    instrument,
    instrument_tool,
    remove_interceptor,
    remove_stream_observer,
    trace,
)
from .types import LLMCall, Money, ToolCall, Usage, sum_usage

__all__ = [
    "LLMCall",
    "ToolCall",
    "Usage",
    "sum_usage",
    "Money",
    "bus",
    "tokens",
    "prices",
    "otel",
    "protocols",
    "instrument",
    "instrument_tool",
    "Reroute",
    # Interceptors: register a pre-call hook (return MISS to proceed untouched). Documented
    # top-level in core.md and exported top-level by @cendor/core — re-exported here for parity.
    "add_interceptor",
    "remove_interceptor",
    "MISS",
    # Ambient metadata seam: register a provider that stamps run context (agent/conversation/…) onto
    # every event at construction — the one correct capture moment (mirror of @cendor/core).
    "add_ambient_provider",
    "ambient_attrs",
    "remove_ambient_provider",
    # Stream-observer seam: a per-chunk observer on instrumented streams; raising aborts the stream
    # (tokenguard's mid-stream budget breaker rides this — core learns no budget vocabulary).
    "add_stream_observer",
    "remove_stream_observer",
    "trace",
    "current_trace_id",
]
