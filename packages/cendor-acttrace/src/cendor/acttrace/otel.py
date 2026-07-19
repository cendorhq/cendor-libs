"""Mirror the audit chain to OpenTelemetry (optional). No-op if OTel isn't installed. See docs.

The mirror is an **operational copy** for monitoring / alerting / SIEM — it lets governance events
(decisions, guardrail actions, policy flags, human oversight, budget breaches) show up in Azure
Monitor / Datadog / CloudWatch / Grafana alongside your traces, so an on-call engineer can *see* and
*alert on* them. It is **not** the evidence: the hash-chained file written by ``AuditLog(path=…)``
remains the only verifiable artifact — ``verify()`` re-walks that file, never the mirror. A mirror
can lag, drop, or be reconfigured without weakening the chain, exactly because it is a copy.

Each entry is emitted as a span named ``audit.<type>`` carrying ``cendor.audit.*`` attributes
(structured labels only — never raw content; the chain has already redacted the payload). It is
parented to whatever span is current when the entry is chained, so an audit entry produced during an
instrumented model call (e.g. under ``cendor.sdk.otel.live_spans``) nests under that call's span and
correlates automatically. Spans are used rather than the still-experimental OpenTelemetry **Logs**
signal so the mirror works unchanged on every current release and matches the rest of the stack
(``core.otel`` spans, the SDK span tree). Wrap it in ``tokenguard.sinks.QueueSink`` if you want its
(already-async) export fully off the append path.
"""

from __future__ import annotations

from typing import Any

#: Structured, non-sensitive payload keys worth surfacing as queryable/alertable span attributes.
#: The chain redacts payloads before they reach the mirror; these are labels (category, action,
#: model, reviewer, …), never raw user content.
_ATTR_KEYS = (
    "decision_id",
    "action",
    "severity",
    "reason",
    "guardrail",
    "stage",
    "provider",
    "model",
    "reviewer",
    "name",
    "actor",
    "data",
    "cost",
    "otel_trace_id",
)


class OTelMirror:
    """An ``AuditLog(mirror=…)`` destination that mirrors each chained entry to OpenTelemetry.

    ```python
    from cendor.acttrace import AuditLog, OTelMirror

    # configure your OTel pipeline once (e.g. azure.monitor.opentelemetry.configure_azure_monitor())
    audit = AuditLog(system="support", path="audit.jsonl", mirror=OTelMirror())
    ```

    A **no-op** when OpenTelemetry isn't installed (``pip install "cendor-core[otel]"``), so it is
    always safe to attach. Pass a specific ``tracer`` to override the default ``cendor.acttrace``.
    """

    def __init__(self, tracer: Any = None) -> None:
        self._tracer: Any = None
        self._system = ""  # learned from the opening entry, then stamped on every span
        try:
            from opentelemetry import trace
        except ImportError:
            return  # OTel not installed — every write() is a silent no-op
        self._tracer = tracer or trace.get_tracer("cendor.acttrace")

    def write(self, entry: Any) -> None:
        """Mirror one :class:`~cendor.acttrace.AuditEntry` as a span. No-op without OTel."""
        if self._tracer is None:
            return
        payload = getattr(entry, "payload", None) or {}
        # audit_open is always the first entry of a fresh log and carries the system name; remember
        # it so every subsequent audit span is filterable by system (e.g. system="support").
        if getattr(entry, "type", "") == "audit_open" and payload.get("system"):
            self._system = str(payload["system"])
        span = self._tracer.start_span(f"audit.{getattr(entry, 'type', 'entry')}")
        try:
            span.set_attribute("cendor.audit.type", str(getattr(entry, "type", "")))
            seq = getattr(entry, "seq", None)
            if seq is not None:
                span.set_attribute("cendor.audit.seq", int(seq))
            digest = getattr(entry, "hash", None)
            if digest:
                span.set_attribute("cendor.audit.hash", str(digest))
            system = self._system or str(payload.get("system", ""))
            if system:
                span.set_attribute("cendor.audit.system", system)
            for key in _ATTR_KEYS:
                value = payload.get(key)
                if value is None or value == "" or value == {} or value == []:
                    continue
                attr = f"cendor.audit.{key}"
                if isinstance(value, (bool, int, float, str)):
                    span.set_attribute(attr, value)
                elif isinstance(value, (list, tuple)) and all(
                    isinstance(item, (bool, int, float, str)) for item in value
                ):
                    span.set_attribute(attr, list(value))
                else:
                    span.set_attribute(attr, str(value))
        finally:
            span.end()  # a point-in-time governance event; duration is not meaningful
