"""cendor.core — the shared foundation. Keep this public surface small and stable."""

from __future__ import annotations

from . import bus, otel, prices, protocols, tokens
from .ambient import add_ambient_provider, remove_ambient_provider
from .instrument import (
    MISS,
    Reroute,
    add_interceptor,
    current_trace_id,
    instrument,
    instrument_tool,
    remove_interceptor,
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
    "remove_ambient_provider",
    "trace",
    "current_trace_id",
]
