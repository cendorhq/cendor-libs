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
    "otel_span_id",  # G12: the correlation span id (pivot target), was stamped but never exposed
)

#: Free-text attributes (``description``/``note``) are truncated to this many characters on the
#: span — the file keeps the full text; the mirror is a bounded operational label.
_TEXT_MAX = 200


def _set_scalar(span: Any, attr: str, value: Any) -> None:
    """Set one scalar span attribute, skipping empties; stringify non-primitives (mirrors the
    generic ``_ATTR_KEYS`` loop's handling for the typed extras below)."""
    if value is None or value == "" or value == {} or value == []:
        return
    if isinstance(value, (bool, int, float, str)):
        span.set_attribute(attr, value)
    else:
        span.set_attribute(attr, str(value))


def _set_int(span: Any, attr: str, value: Any) -> None:
    """Set an int span attribute when ``value`` is a real number (skip None/non-numeric/bool)."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        span.set_attribute(attr, int(value))


def _set_text(span: Any, attr: str, value: Any) -> None:
    """Set a truncated free-text attribute (``description``/``note``); skip empties."""
    if value is None or value == "":
        return
    text = str(value)
    if len(text) > _TEXT_MAX:
        text = text[: _TEXT_MAX - 1] + "…"
    span.set_attribute(attr, text)


def _flatten_tags(span: Any, tags: Any) -> None:
    """Flatten ``track()`` attribution tags as ``cendor.audit.tag.<key>`` (bounded values only)."""
    if not isinstance(tags, dict):
        return
    for key, value in tags.items():
        if value is None or value == "":
            continue
        attr = f"cendor.audit.tag.{key}"
        if isinstance(value, (bool, int, float, str)):
            span.set_attribute(attr, value)
        else:
            span.set_attribute(attr, str(value))


def _block_counts(span: Any, decisions: Any) -> None:
    """Count context-assembly block decisions by action and set the non-zero counts (G16). A
    ``compressed`` count > 0 is squeeze's indirect visibility on the wire."""
    if not isinstance(decisions, (list, tuple)):
        return
    counts: dict[str, int] = {}
    for d in decisions:
        action = d.get("action") if isinstance(d, dict) else None
        if action:
            counts[str(action)] = counts.get(str(action), 0) + 1
    for action in ("kept", "truncated", "summarized", "compressed", "dropped"):
        n = counts.get(action, 0)
        if n:
            span.set_attribute(f"cendor.audit.{action}", n)


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
            etype = str(getattr(entry, "type", ""))
            for key in _ATTR_KEYS:
                # A budget's `name` is exposed as `cendor.audit.budget` (below), not the generic
                # `cendor.audit.name`, so a monitor queries one clear attribute for the budget name.
                if key == "name" and etype == "budget_event":
                    continue
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
            # --- Typed / nested handling (G11/G12/G16): fields the flat loop above can't reach
            # (nested usage/metadata dicts, renamed keys, per-action counts). Values still derive
            # only from the already-scrubbed payload — never raw content.
            if etype == "budget_event":  # G11: budget identity + numeric projected-vs-cap
                _set_scalar(span, "cendor.audit.budget", payload.get("name"))
                _set_text(span, "cendor.audit.description", payload.get("description"))
                _set_scalar(span, "cendor.audit.scope", payload.get("scope"))
                _set_scalar(span, "cendor.audit.to_model", payload.get("to_model"))
                _set_scalar(span, "cendor.audit.projected_usd", payload.get("projected_usd"))
                _set_scalar(span, "cendor.audit.cap_usd", payload.get("cap_usd"))
                _set_int(span, "cendor.audit.projected_tokens", payload.get("projected_tokens"))
                _set_int(span, "cendor.audit.cap_tokens", payload.get("cap_tokens"))
                _flatten_tags(span, payload.get("tags"))
            elif etype == "llm_call":  # G12: token usage / latency / cassette replay flag
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    _set_int(span, "cendor.audit.input_tokens", usage.get("input_tokens"))
                    _set_int(span, "cendor.audit.output_tokens", usage.get("output_tokens"))
                    _set_int(span, "cendor.audit.reasoning_tokens", usage.get("reasoning_tokens"))
                _set_scalar(span, "cendor.audit.latency_ms", payload.get("latency_ms"))
                span.set_attribute("cendor.audit.replayed", bool(payload.get("replayed", False)))
            elif etype == "guardrail_decision":  # G12: agent/tool + policy provenance from metadata
                _set_scalar(span, "cendor.audit.agent", payload.get("agent"))
                _set_scalar(span, "cendor.audit.tool", payload.get("tool"))
                meta = payload.get("metadata")
                if isinstance(meta, dict):
                    # severity here is the guardrail's own nested severity (the top-level _ATTR_KEYS
                    # `severity` only ever matched a policy_flag — the bug the fit-gap flagged).
                    _set_scalar(span, "cendor.audit.severity", meta.get("severity"))
                    _set_scalar(span, "cendor.audit.policy_version", meta.get("policy_version"))
                    _set_scalar(span, "cendor.audit.policy_hash", meta.get("policy_hash"))
            elif etype == "human_oversight":  # G12: the reviewer's note
                _set_text(span, "cendor.audit.note", payload.get("note"))
            elif etype == "audit_open":  # G12: the chain's declared risk tier
                _set_scalar(span, "cendor.audit.risk_tier", payload.get("risk_tier"))
            elif etype == "context_assembly":  # G16: budget math + per-action block counts
                _set_int(span, "cendor.audit.budget_tokens", payload.get("budget"))
                _set_int(span, "cendor.audit.used_tokens", payload.get("used"))
                _block_counts(span, payload.get("decisions"))
            elif etype == "compression":  # G21: squeeze technique + token savings (metadata only)
                _set_scalar(span, "cendor.audit.technique", payload.get("technique"))
                _set_int(span, "cendor.audit.tokens_before", payload.get("tokens_before"))
                _set_int(span, "cendor.audit.tokens_after", payload.get("tokens_after"))
                _set_scalar(span, "cendor.audit.ratio", payload.get("ratio"))
                _set_scalar(span, "cendor.audit.store_kind", payload.get("store_kind"))
                _set_scalar(span, "cendor.audit.handle_id", payload.get("handle_id"))
                _set_scalar(span, "cendor.audit.kind", payload.get("kind"))
        finally:
            span.end()  # a point-in-time governance event; duration is not meaningful
