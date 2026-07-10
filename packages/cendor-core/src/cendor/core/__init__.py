"""cendor.core — the shared foundation. Keep this public surface small and stable."""

from __future__ import annotations

from . import bus, otel, prices, protocols, tokens
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
from .types import LLMCall, Money, ToolCall, Usage

__all__ = [
    "LLMCall",
    "ToolCall",
    "Usage",
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
    "trace",
    "current_trace_id",
]
