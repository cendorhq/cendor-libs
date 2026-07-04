"""cendor.acttrace — a tamper-evident, auto-populated audit log for AI decisions.

Construct an :class:`AuditLog` and it **subscribes** to ``cendor.core``'s event stream: every
instrumented model/tool call — and the context decisions ``contextkit`` and cost ``tokenguard``
ride on that same stream — becomes an audit entry with no per-call wiring. You add only the
explicit human-facing events (``decision``, ``human_oversight``).

Integrity comes from a **hash chain**, not a server: ``entry.hash = sha256(prev_hash +
canonical(entry))``, so editing any past entry breaks every entry after it. ``acttrace verify
file.jsonl`` re-walks the chain offline.

> This produces **evidence to support** compliance (e.g. EU AI Act record-keeping / human
> oversight). It is **not** legal advice and not a compliance guarantee. Control mappings are a
> starting template for your compliance team to adjust.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cendor.core import bus
from cendor.core.types import LLMCall, ToolCall

from .detectors import (
    DETECTORS,
    Detector,
    _scan_counts,
    _scrub,
    detectors,
    group_of,
    register_detector,
)
from .guard import PolicyViolation, guard
from .ner import ner_available, ner_redactor
from .packs import LOCALE_PACKS, enable_entropy_detector, enable_locale_pack
from .policy import Finding, Policy, redact, scan

__all__ = [
    "AuditLog",
    "AuditEntry",
    "verify",
    "frameworks",
    "default_redactor",
    "GENESIS",
    # detection & policy (roadmap phase 1)
    "Detector",
    "DETECTORS",
    "register_detector",
    "detectors",
    "Policy",
    "Finding",
    "scan",
    "redact",
    # enforcement (roadmap phase 2)
    "guard",
    "PolicyViolation",
    # optional power — all opt-in (roadmap phase 3)
    "enable_locale_pack",
    "enable_entropy_detector",
    "LOCALE_PACKS",
    "ner_available",
    "ner_redactor",
]

GENESIS = "0" * 64

#: Recommended vocabularies for a policy flag (normalized to lowercase; other strings are allowed).
FlagAction = Literal["flagged", "redacted", "blocked"]
FlagSeverity = Literal["info", "warning", "critical"]

_active_decision: ContextVar[str | None] = ContextVar("cendor_acttrace_decision", default=None)

# Starting-template control mappings (NOT legal advice; adjust for your system). docs §5, §7.
# event type -> framework control IDs. Used by export(framework=...) to annotate the evidence pack.
# Control IDs reference the public framework texts (EU AI Act Reg. 2024/1689; NIST AI RMF 1.0;
# ISO/IEC 42001:2023 Annex A; GDPR Reg. 2016/679) — they map an event to the controls it provides
# *evidence for*, never a claim of compliance. Your compliance team owns the final mapping.
_CONTROLS: dict[str, dict[str, list[str]]] = {
    "eu_ai_act": {
        "audit_open": ["Art.12 record-keeping", "Art.19 automatically generated logs"],
        "decision": ["Art.12 record-keeping", "Art.13 transparency"],
        "decision_record": ["Art.12 record-keeping", "Art.13 transparency"],
        "decision_end": ["Art.12 record-keeping"],
        "llm_call": [
            "Art.12 logging",
            "Art.19 automatically generated logs",
            "Art.72 post-market monitoring",
        ],
        "tool_call": ["Art.12 logging", "Art.19 automatically generated logs"],
        "context_assembly": ["Art.12 logging", "Art.13 transparency"],
        "human_oversight": ["Art.14 human oversight", "Art.26(5) deployer oversight"],
        "policy_flag": ["Art.10 data governance", "Art.12 record-keeping"],
    },
    "nist_rmf": {
        "audit_open": ["GOVERN-1.1"],
        "decision": ["MAP-1.1", "MEASURE-2.1"],
        "decision_record": ["MEASURE-2.1"],
        "decision_end": ["MEASURE-2.1"],
        "llm_call": ["MEASURE-2.1"],
        "tool_call": ["MEASURE-2.1"],
        "context_assembly": ["MEASURE-2.1"],
        "human_oversight": ["MANAGE-2.1"],
        "policy_flag": ["MANAGE-2.1", "MEASURE-2.1"],
    },
    "iso_42001": {  # ISO/IEC 42001:2023 Annex A controls + management clauses
        "audit_open": ["A.6.2.8 event logs"],
        "decision": ["A.6.2.8 event logs", "A.5.2 AI system impact assessment"],
        "decision_record": ["A.6.2.8 event logs"],
        "decision_end": ["A.6.2.8 event logs"],
        "llm_call": [
            "A.6.2.8 event logs",
            "A.6.2.6 operation & monitoring",
            "Cl.9.1 monitoring & measurement",
        ],
        "tool_call": ["A.6.2.8 event logs", "A.6.2.6 operation & monitoring"],
        "context_assembly": ["A.6.2.8 event logs", "A.6.2.6 operation & monitoring"],
        "human_oversight": ["A.9.2 responsible use", "A.9.4 intended use"],
        "policy_flag": ["A.7 data for AI systems", "A.6.2.8 event logs", "A.9.2 responsible use"],
    },
    "gdpr": {  # automated decision-making + records of processing (Reg. 2016/679)
        "audit_open": ["Art.30 records of processing", "Art.5(2) accountability"],
        "decision": ["Art.22 automated decision-making", "Art.5(2) accountability"],
        "decision_record": ["Art.22 automated decision-making"],
        "decision_end": ["Art.30 records of processing"],
        "llm_call": ["Art.30 records of processing"],
        "tool_call": ["Art.30 records of processing"],
        "context_assembly": ["Art.30 records of processing"],
        "human_oversight": ["Art.22(3) right to human intervention"],
        "policy_flag": [
            "Art.9 special-category data",
            "Art.5(1)(c) data minimisation",
            "Art.30 records of processing",
        ],
    },
}


# Category/group-specific control pointers, layered *on top of* the per-type ``policy_flag`` mapping
# above when a flag names the category it fired on (``data=[...]``). This makes a category-tagged
# flag point at the specific control it is evidence for (e.g. a special-category flag → GDPR Art.9,
# a card-data flag → the PCI-DSS pointer) rather than the generic data-governance bucket. Keyed by
# detector group or specific category; still evidence pointers, never a compliance claim. The PCI
# entries are cross-references (PCI-DSS is not a bundled export framework) surfaced under the data-
# protection frameworks where card handling is in scope.
_CATEGORY_CONTROLS: dict[str, dict[str, list[str]]] = {
    "special_category": {
        "gdpr": ["Art.9 special-category data"],
        "eu_ai_act": ["Art.10(5) special categories for bias detection"],
    },
    "gov_id": {
        "gdpr": ["Art.87 processing of national identification numbers"],
    },
    "financial": {
        "gdpr": ["PCI-DSS 3.3/3.4 (payment-card data — cross-reference)"],
        "eu_ai_act": ["PCI-DSS 3.3/3.4 (payment-card data — cross-reference)"],
    },
    "pii": {
        "gdpr": ["Art.4(1) personal data", "Art.5(1)(c) data minimisation"],
    },
    "secret": {
        "gdpr": ["Art.32 security of processing"],
    },
    "credential": {
        "gdpr": ["Art.32 security of processing"],
    },
}


def frameworks() -> list[str]:
    """Frameworks with a bundled (starting-template) control mapping for :meth:`AuditLog.export`."""
    return sorted(_CONTROLS)


def _controls_for_entry(entry: AuditEntry, framework: str, controls: dict[str, list[str]]) -> list:
    """Control IDs an entry is evidence for: the per-type mapping, plus category-specific pointers
    for a ``policy_flag`` whose ``data`` names the categories it fired on (additive, deduped)."""
    result = list(controls.get(entry.type, []))
    if entry.type != "policy_flag":
        return result
    data = entry.payload.get("data")
    cats = data if isinstance(data, list) else ([data] if isinstance(data, str) else [])
    for cat in cats:
        for key in (cat, group_of(cat) if isinstance(cat, str) else None):
            if key is None:
                continue
            for control in _CATEGORY_CONTROLS.get(key, {}).get(framework, []):
                if control not in result:
                    result.append(control)
    return result


@dataclass
class AuditEntry:
    """One link in the hash chain. docs/acttrace.md §5."""

    seq: int
    ts: str
    type: str  # decision | llm_call | tool_call | human_oversight | context_assembly | ...
    payload: dict
    prev_hash: str
    hash: str
    sig: str = ""  # HMAC-SHA256 of `hash` under the signing key, if the log is signed


#: Entry types that carry caller-supplied content (where PII actually lands), so a detection in one
#: of them is worth a follow-up flag. Excludes structural entries (audit_open / decision_end), the
#: flag itself (no recursion), and human_oversight (a reviewer's identity is legitimate audit data,
#: not PII to flag). Note: llm_call stores only metadata — messages are never recorded — so PII most
#: often surfaces in a decision's input or a tool_call's arguments.
_AUTO_REDACT_TYPES = frozenset(
    {"decision", "decision_record", "llm_call", "tool_call", "context_assembly"}
)

#: Snapshot of the default policy backing :data:`default_redactor` (secrets & email → redact). The
#: registry is still consulted live, so a :func:`register_detector` in group ``secret``/``pii`` is
#: picked up here too.
_DEFAULT_POLICY = Policy.default()

#: Verb recorded on an auto-flag for each resolved action, and the deterministic emit order
#: (most-severe first). ``allow`` never produces a flag.
_ACTION_VERB = {"block": "blocked", "redact": "redacted", "flag": "flagged"}
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _redact(obj: Any) -> Any:
    """The built-in scrubber: remove every ``redact``/``block`` category under the default policy.

    Rebuilt from the :mod:`~cendor.acttrace.detectors` registry, so the original six categories
    (emails / ``sk-`` keys incl. ``sk-ant-``/``sk-proj-`` / AWS + Google API keys / JWTs / bearer
    tokens) scrub byte-for-byte as before, and any newly-registered secret is covered too.
    """
    cleaned, _findings = redact(obj, _DEFAULT_POLICY)
    return cleaned


#: The built-in redactor. Exposed so a custom ``redactor`` can compose it:
#: ``AuditLog(redactor=lambda o: my_scrub(default_redactor(o)))``.
default_redactor = _redact


def _max_severity(severities: Any) -> str:
    """The most serious of the given severities (``info`` < ``warning`` < ``critical``)."""
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0), default="warning")


def _auto_flags(
    counts: dict[str, tuple[Detector, int]], policy: Policy, etype: str
) -> list[tuple[str, str, str, list[str]]]:
    """Group detected categories by resolved action into ``(reason, action, severity, data)`` rows.

    One row per non-``allow`` action, emitted most-severe first. The ``redacted`` row keeps its
    historical ``severity="info"`` (scrubbing is a benign safety net); ``flagged``/``blocked`` rows
    carry the strongest detector severity in the group.
    """
    by_action: dict[str, list[tuple[str, str]]] = {}
    for _cat, (det, _n) in counts.items():
        action = policy.action_for(det.category, det.group)
        if action == "allow":
            continue
        by_action.setdefault(action, []).append((det.category, det.severity))
    rows: list[tuple[str, str, str, list[str]]] = []
    for action in ("block", "redact", "flag"):
        items = by_action.get(action)
        if not items:
            continue
        cats = sorted({c for c, _s in items})
        verb = _ACTION_VERB[action]
        severity = "info" if action == "redact" else _max_severity(s for _c, s in items)
        rows.append((f"{verb} {', '.join(cats)} from {etype}", verb, severity, cats))
    return rows


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "amount") and hasattr(obj, "currency"):  # Money
        return f"{obj.amount} {obj.currency}"
    if hasattr(obj, "__dict__"):
        return _jsonable(vars(obj))
    return str(obj)


def _canonical(payload: dict) -> str:
    return json.dumps(_jsonable(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _chain_hash(prev_hash: str, seq: int, ts: str, etype: str, payload: dict) -> str:
    body = _canonical({"seq": seq, "ts": ts, "type": etype, "payload": payload})
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def _meta_signature(key_bytes: bytes, meta: dict) -> str:
    """HMAC over an export ``_meta`` header's completeness fields, so the header itself is
    tamper-evident. Without this, an attacker who drops trailing entries and rewrites
    ``head_hash``/``entries`` would pass :func:`verify` — the chain is only as strong as the
    completeness claim, and that claim lives in the same (previously unsigned) file."""
    body = _canonical(
        {
            "system": meta.get("system"),
            "risk_tier": meta.get("risk_tier"),
            "head_hash": meta.get("head_hash"),
            "entries": meta.get("entries"),
        }
    )
    return hmac.new(key_bytes, body.encode("utf-8"), hashlib.sha256).hexdigest()


class AuditLog:
    """A hash-chained, append-only, auto-populating audit log. docs/acttrace.md §3, §5."""

    def __init__(
        self,
        system: str,
        risk_tier: str = "limited",
        path: str | None = None,
        signing_key: str | bytes | None = None,
        redact: bool = True,
        redactor: Callable[[Any], Any] | None = None,
        flag_on_redact: bool = True,
        policy: Policy | None = None,
    ) -> None:
        """``policy`` selects the detection posture (see :class:`~cendor.acttrace.Policy`): every
        auto-captured payload is scanned against the full detector registry and, per the policy,
        ``redact``/``block`` categories are scrubbed and detections are auto-flagged with their
        resolved action/severity. Defaults to :meth:`Policy.default` (secrets & email redacted,
        everything else flagged) — identical to previous behaviour. ``redact=True`` ⇒
        ``Policy.default()``; ``redact=False`` disables scanning/scrubbing entirely.

        ``redactor`` overrides the built-in scrubber: a ``payload -> payload`` callable applied
        before each entry is chained/written (compose :data:`default_redactor` to extend it). A
        custom ``redactor`` bypasses the policy engine and owns its own flagging.

        ``flag_on_redact`` (default ``True``): when the built-in path acts on sensitive data in an
        auto-captured entry, also append a ``policy_flag`` recording *what category* and *what
        action* — so "we removed / flagged PII" is itself in the tamper-evident chain, not silent.
        Only fires with the built-in path (a custom ``redactor`` owns its own flagging)."""
        self.system = system
        self.risk_tier = risk_tier
        self.path = Path(path) if path else None
        self._signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key
        # A policy (explicit or from redact=True) turns on scanning/scrubbing; redact=False with no
        # policy turns it off. The policy drives both what is scrubbed and what is auto-flagged.
        self._redact = redact or policy is not None
        self._policy = policy or Policy.default()
        self._redactor = redactor or _redact  # _redact sentinel => built-in policy-driven path
        self._flag_on_redact = flag_on_redact
        self.entries: list[AuditEntry] = []
        self._head = GENESIS
        # RLock (not Lock): _append can re-enter itself via the auto-redaction flag() on the same
        # thread. Guards the hash-chain critical section (head + entries + file append) so
        # concurrent bus emits can't corrupt the chain.
        self._lock = threading.RLock()
        self._fh: Any = None  # kept-open append handle: durable, and avoids reopen-per-entry cost
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("w", encoding="utf-8")  # truncate, then hold the handle open
        self._append("audit_open", {"system": system, "risk_tier": risk_tier})
        bus.subscribe(self._on_event)

    @property
    def head(self) -> str:
        """The current chain head hash. Capture it to later assert completeness:
        ``verify(path, expected_head=log.head)`` catches trailing entries being dropped."""
        return self._head

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.detach()  # stop subscribing when the block exits, so logs don't leak onto the bus

    # ------------------------------------------------------------------ chain

    def _append(self, etype: str, payload: dict) -> AuditEntry:
        with self._lock:  # hash-chain step is a read-modify-write on _head/entries/file — atomic
            seq = len(self.entries)
            ts = datetime.now(UTC).isoformat()
            safe = _jsonable(payload)
            auto_flags: list[tuple[str, str, str, list[str]]] = []
            if self._redact:
                if self._redactor is _redact:  # built-in path: policy-driven scan → scrub → flag
                    counts = _scan_counts(safe)  # detect on the pre-scrub view
                    if counts:
                        scrub = {
                            cat
                            for cat, (det, _n) in counts.items()
                            if self._policy.action_for(det.category, det.group)
                            in ("redact", "block")
                        }
                        if scrub:
                            safe = _scrub(safe, scrub)  # scrub before hashing → chain is consistent
                        if etype in _AUTO_REDACT_TYPES and self._flag_on_redact:
                            auto_flags = _auto_flags(counts, self._policy, etype)
                else:
                    safe = self._redactor(safe)  # custom redactor owns scrubbing + its own flagging
            h = _chain_hash(self._head, seq, ts, etype, safe)
            sig = ""
            if self._signing_key is not None:
                sig = hmac.new(self._signing_key, h.encode("utf-8"), hashlib.sha256).hexdigest()
            entry = AuditEntry(seq, ts, etype, safe, self._head, h, sig)
            self.entries.append(entry)
            self._head = h
            self._write_line(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
        for reason, action, severity, data in auto_flags:
            # append a follow-up policy_flag so the detection is itself in the chain. The flag's own
            # _append carries etype="policy_flag" (not an auto type), so this never recurses.
            self.flag(reason, action=action, severity=severity, data=data, auto=True)
        return entry

    def _write_line(self, line: str) -> None:
        """Durably append one JSONL line (caller holds ``self._lock``).

        Uses the kept-open handle while the log is active (avoids the ~10× reopen-per-entry cost);
        after :meth:`detach` has closed it, falls back to open-append-close so a detached log still
        persists and leaves no handle holding the file open."""
        if self.path is None:
            return
        if self._fh is not None:
            self._fh.write(line)
            self._fh.flush()  # visible to a concurrent verify() immediately
            os.fsync(self._fh.fileno())  # crash-durable: survives a process/power loss
        else:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    # ------------------------------------------------------------------ auto-capture

    def _on_event(self, event: Any) -> None:
        did = _active_decision.get()
        if isinstance(event, LLMCall):
            self._append(
                "llm_call",
                {
                    "decision_id": did,
                    "provider": event.provider,
                    "model": event.model,
                    "usage": _jsonable(event.usage),
                    "cost": _jsonable(event.cost),
                    "latency_ms": event.latency_ms,
                    "replayed": event.metadata.get("replayed", False),
                },
            )
        elif isinstance(event, ToolCall):
            self._append(
                "tool_call",
                {"decision_id": did, "name": event.name, "arguments": _jsonable(event.arguments)},
            )
        elif hasattr(event, "decisions") and hasattr(event, "budget"):  # contextkit AssemblyReport
            self._append(
                "context_assembly",
                {
                    "decision_id": did,
                    "model": getattr(event, "model", None),
                    "budget": event.budget,
                    "used": getattr(event, "used", None),
                    "decisions": _jsonable(event.decisions),
                },
            )

    def detach(self) -> None:
        """Stop subscribing to the core event stream and close the log file handle (idempotent)."""
        bus.unsubscribe(self._on_event)
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
                self._fh = None

    # ------------------------------------------------------------------ explicit events

    @contextmanager
    def decision(self, input: Any = None, actor: str = "agent") -> Iterator[Decision]:
        """Group a unit of work. Auto-captured calls inside it are tagged with this decision."""
        did = uuid.uuid4().hex
        self._append("decision", {"decision_id": did, "input": _jsonable(input), "actor": actor})
        token = _active_decision.set(did)
        try:
            yield Decision(self, did)
        finally:
            _active_decision.reset(token)
            self._append("decision_end", {"decision_id": did})

    def flag(
        self,
        reason: str,
        *,
        action: str = "flagged",
        severity: str = "warning",
        data: Any = None,
        **fields: Any,
    ) -> AuditEntry:
        """Record a policy flag — e.g. input a guard decided should not be processed by the agent.

        A tamper-evident record that a data/usage policy fired. ``action`` is what your guard did
        (``"flagged"`` | ``"redacted"`` | ``"blocked"``), ``reason`` why, ``severity`` how serious
        (``"info"`` | ``"warning"`` | ``"critical"``), and ``data`` a *summary/category* of the
        offending content — pass a label, **never the raw sensitive value** (it is chained and
        written; redaction still runs over it). ``action``/``severity`` are normalized to lowercase;
        other strings are accepted, not rejected. Auto-tags the active :meth:`decision` span.

        acttrace only *records* the flag; deciding and enforcing the policy is your guard's job —
        typically a pre-flight ``core.add_interceptor`` that inspects the request and raises to
        block it (see docs/acttrace.md). Recorder and enforcer stay separate by design.
        """
        return self._append(
            "policy_flag",
            {
                "decision_id": _active_decision.get(),
                "reason": reason,
                "action": str(action).lower(),
                "severity": str(severity).lower(),
                "data": data,
                **fields,
            },
        )

    # ------------------------------------------------------------------ export

    def _summary(self) -> dict:
        """Substance counts for the evidence-pack header: how many decisions, calls, oversight
        events and flags (broken down by action/severity) — what a reviewer scans first."""
        types = Counter(e.type for e in self.entries)
        flags = [e for e in self.entries if e.type == "policy_flag"]
        return {
            "decisions": types.get("decision", 0),
            "llm_calls": types.get("llm_call", 0),
            "tool_calls": types.get("tool_call", 0),
            "context_assemblies": types.get("context_assembly", 0),
            "human_oversight": types.get("human_oversight", 0),
            "policy_flags": len(flags),
            "flags_by_action": dict(Counter(e.payload.get("action") for e in flags)),
            "flags_by_severity": dict(Counter(e.payload.get("severity") for e in flags)),
        }

    def export(self, path: str, framework: str | None = None) -> None:
        """Write the chain as a JSONL evidence pack, optionally annotated with control IDs.

        ``framework`` (e.g. ``"eu_ai_act"`` or ``"nist_rmf"``) annotates each entry with the
        control IDs it provides evidence for, and the ``_meta`` header lists every control covered.
        Mappings are starting templates, not legal advice. See :func:`frameworks`.
        """
        if framework and framework not in _CONTROLS:
            raise ValueError(f"unknown framework {framework!r}; available: {frameworks()}")
        controls = _CONTROLS.get(framework or "", {})
        covered = sorted(
            {c for e in self.entries for c in _controls_for_entry(e, framework or "", controls)}
        )
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            meta_body = {
                "system": self.system,
                "risk_tier": self.risk_tier,
                "framework": framework,
                "controls_covered": covered,
                "summary": self._summary(),
                "head_hash": self._head,
                "entries": len(self.entries),
                "disclaimer": "Evidence to support compliance — not legal advice.",
            }
            if self._signing_key is not None:
                # Sign the completeness fields so a rewritten header (dropped tail + faked
                # head_hash/entries) is caught by verify(key=...). docs/acttrace.md §5.
                meta_body["sig"] = _meta_signature(self._signing_key, meta_body)
            fh.write(json.dumps({"_meta": meta_body}, ensure_ascii=False) + "\n")
            for entry in self.entries:
                row = dict(entry.__dict__)
                if framework:
                    row["controls"] = _controls_for_entry(entry, framework, controls)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@dataclass
class Decision:
    """Handle for the active decision span (yielded by :meth:`AuditLog.decision`)."""

    log: AuditLog
    id: str

    def record(self, **fields: Any) -> None:
        """Record decision metadata (e.g. ``model``, ``prompt_id``)."""
        self.log._append("decision_record", {"decision_id": self.id, **fields})

    def human_oversight(self, reviewer: str, action: str, note: str = "") -> None:
        """Record an Art. 14-style human-oversight event: who reviewed, what action, when."""
        self.log._append(
            "human_oversight",
            {"decision_id": self.id, "reviewer": reviewer, "action": action, "note": note},
        )

    def flag(
        self,
        reason: str,
        *,
        action: str = "flagged",
        severity: str = "warning",
        data: Any = None,
        **fields: Any,
    ) -> AuditEntry:
        """Record a policy flag tagged to this decision (see :meth:`AuditLog.flag`). Returns the
        chained :class:`AuditEntry` (matching :meth:`AuditLog.flag`)."""
        return self.log._append(
            "policy_flag",
            {
                "decision_id": self.id,
                "reason": reason,
                "action": str(action).lower(),
                "severity": str(severity).lower(),
                "data": data,
                **fields,
            },
        )


def verify(
    path: str,
    *,
    key: str | bytes | None = None,
    expected_head: str | None = None,
    expect_entries: int | None = None,
) -> tuple[bool, str]:
    """Re-walk the hash chain in a JSONL file. Returns ``(ok, detail)``. docs/acttrace.md §5.

    Detects edits and deletions, *including tail-truncation*: a hash chain alone can't catch
    trailing entries being dropped, so completeness is checked against an expected head hash and/or
    entry count. An exported pack's ``_meta`` header (``head_hash``/``entries``) is used
    automatically; ``expected_head`` / ``expect_entries`` override it (capture :attr:`AuditLog.head`
    for a raw log).

    **Trust boundary.** Without ``key``, the in-file ``_meta`` header is *unauthenticated*: an
    attacker who drops trailing entries and rewrites ``_meta``'s ``head_hash``/``entries`` passes an
    in-file-only completeness check. So the returned ``detail`` says completeness rested on in-file
    metadata, and an **out-of-band** ``expected_head`` / ``expect_entries`` (captured from
    :attr:`AuditLog.head` at write time) is authoritative. With ``key``, ``_meta`` must carry a
    valid HMAC signature (written by :meth:`AuditLog.export` on a signed log) — a stripped or forged
    header fails verification, closing the rewrite hole.

    If ``key`` is given, also verify each entry's HMAC signature against it (proving the log was
    produced by a holder of the key, not just internal consistency). Streams the file, so memory
    stays flat on large logs. Never raises on a missing/corrupt file — returns ``(False, detail)``.
    """
    key_bytes = key.encode() if isinstance(key, str) else key
    prev = GENESIS
    seen = 0
    meta: dict | None = None
    try:
        # Iterate the file object: universal newlines split on the record separator only — NOT on
        # the Unicode line separators (U+2028 / U+0085 / …) that str.splitlines() would break on.
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                row = json.loads(line)
                if "_meta" in row:  # export header, not a chain entry
                    meta = row["_meta"] if isinstance(row["_meta"], dict) else {}
                    continue
                expected = _chain_hash(prev, row["seq"], row["ts"], row["type"], row["payload"])
                if row["prev_hash"] != prev:
                    return False, f"broken link at seq {row['seq']}: prev_hash mismatch"
                if row["hash"] != expected:
                    return False, f"tampered entry at seq {row['seq']}: hash mismatch"
                if key_bytes is not None:
                    want = hmac.new(
                        key_bytes, row["hash"].encode("utf-8"), hashlib.sha256
                    ).hexdigest()
                    if not hmac.compare_digest(row.get("sig", ""), want):
                        return False, f"bad signature at seq {row['seq']}"
                prev = row["hash"]
                seen += 1
    except (OSError, UnicodeDecodeError) as e:
        return False, f"cannot read {path}: {e}"
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return False, f"corrupt log {path}: {e}"

    meta_head = meta.get("head_hash") if meta is not None else None
    meta_entries = meta.get("entries") if meta is not None else None

    # With a key, the completeness header must itself be authenticated. Reaching here with a key
    # means every entry's signature checked out (a signed log), so a present-but-unsigned or
    # sig-mismatched _meta is tampering — refuse rather than trust a forgeable header.
    meta_trusted = False
    if key_bytes is not None and meta is not None:
        provided_sig = meta.get("sig") or ""
        if not provided_sig:
            return False, "unauthenticated _meta: signed log but header carries no signature"
        if not hmac.compare_digest(provided_sig, _meta_signature(key_bytes, meta)):
            return False, "forged _meta: header signature mismatch (completeness fields altered?)"
        meta_trusted = True

    want_head = expected_head if expected_head is not None else meta_head
    if want_head is not None and prev != want_head:
        return False, (
            f"incomplete log: head {prev[:12]}… != expected {want_head[:12]}… "
            "(trailing entries removed?)"
        )
    want_n = expect_entries if expect_entries is not None else meta_entries
    if want_n is not None and seen != want_n:
        return False, f"incomplete log: found {seen} entries, expected {want_n} (entries removed?)"

    notes = []
    if key_bytes is not None:
        notes.append("signatures verified")
        if meta_trusted:
            notes.append("metadata signature verified")
    elif meta is not None and expected_head is None and expect_entries is None:
        notes.append(
            "completeness from unauthenticated in-file _meta — pass expected_head/"
            "expect_entries out-of-band for an authoritative check"
        )
    suffix = f" ({'; '.join(notes)})" if notes else ""
    return True, f"ok: {seen} entries, head {prev[:12]}…{suffix}"
