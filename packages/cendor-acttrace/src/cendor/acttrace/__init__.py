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
import re
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

__all__ = ["AuditLog", "AuditEntry", "verify", "frameworks", "default_redactor", "GENESIS"]

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


def frameworks() -> list[str]:
    """Frameworks with a bundled (starting-template) control mapping for :meth:`AuditLog.export`."""
    return sorted(_CONTROLS)


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


# Targeted PII/secret patterns (GDPR), each with a category label. Deliberately narrow — every
# pattern is prefix-anchored (`sk-`, `AKIA`, `AIza`, `eyJ`, `Bearer`) so it does NOT touch
# ids/hashes/uuids (the chain's own hex hashes never match). Kept consistent with cassette's
# `_REDACTIONS`. The category labels are what an auto-emitted policy_flag records.
_REDACTION_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # openai sk- keys incl. the hyphenated modern forms (sk-ant-…, sk-proj-…) + legacy sk-…
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),  # AWS access key id
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),  # Google API key
    # bare JSON Web Token (three base64url segments)
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._-]+\b")),
]
_REDACTIONS = [pat for _, pat in _REDACTION_CATEGORIES]


def _redact(obj: Any) -> Any:
    if isinstance(obj, str):
        out = obj
        for pat in _REDACTIONS:
            out = pat.sub("<redacted>", out)
        return out
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _scan_redactions(obj: Any) -> set[str]:
    """Categories of sensitive data (``email`` / ``api_key`` / ``aws_key`` / ``google_api_key`` /
    ``jwt`` / ``bearer_token``) present anywhere in ``obj`` — what the built-in redactor would
    scrub. Drives the auto policy_flag on redaction."""
    found: set[str] = set()

    def walk(o: Any) -> None:
        if isinstance(o, str):
            for cat, pat in _REDACTION_CATEGORIES:
                if pat.search(o):
                    found.add(cat)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found


#: Entry types that carry caller-supplied content (where PII actually lands), so a redaction in one
#: of them is worth a follow-up flag. Excludes structural entries (audit_open / decision_end), the
#: flag itself (no recursion), and human_oversight (a reviewer's identity is legitimate audit data,
#: not PII to flag). Note: llm_call stores only metadata — messages are never recorded — so PII most
#: often surfaces in a decision's input or a tool_call's arguments.
_AUTO_REDACT_TYPES = frozenset(
    {"decision", "decision_record", "llm_call", "tool_call", "context_assembly"}
)


#: The built-in redactor (emails / ``sk-`` keys incl. ``sk-ant-``/``sk-proj-`` / AWS + Google API
#: keys / JWTs / bearer tokens). Exposed so a custom ``redactor`` can compose it:
#: ``AuditLog(redactor=lambda o: my_scrub(default_redactor(o)))``.
default_redactor = _redact


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
    ) -> None:
        """``redactor`` overrides the built-in scrubber: a ``payload -> payload`` callable applied
        before each entry is chained/written (compose :data:`default_redactor` to extend it). Runs
        only when ``redact=True``.

        ``flag_on_redact`` (default ``True``): when the built-in redactor scrubs sensitive data from
        an auto-captured entry, also append a ``policy_flag`` recording *what category* was redacted
        — so "we removed PII" is itself in the tamper-evident chain, not silent. Only fires with the
        built-in redactor (a custom ``redactor`` owns its own flagging)."""
        self.system = system
        self.risk_tier = risk_tier
        self.path = Path(path) if path else None
        self._signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key
        self._redact = redact  # scrub emails/keys/tokens from payloads (GDPR); on by default
        self._redactor = redactor or _redact  # the scrubber used when redaction is on
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
            redacted_categories: set[str] = set()
            if self._redact:
                if self._redactor is _redact and etype in _AUTO_REDACT_TYPES:
                    redacted_categories = _scan_redactions(safe)  # detect on the pre-scrub view
                safe = self._redactor(safe)  # scrub before hashing so the chain is consistent
            h = _chain_hash(self._head, seq, ts, etype, safe)
            sig = ""
            if self._signing_key is not None:
                sig = hmac.new(self._signing_key, h.encode("utf-8"), hashlib.sha256).hexdigest()
            entry = AuditEntry(seq, ts, etype, safe, self._head, h, sig)
            self.entries.append(entry)
            self._head = h
            self._write_line(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
        if redacted_categories and self._flag_on_redact:
            # append a follow-up policy_flag so the redaction is itself in the chain. The flag's own
            # _append carries etype="policy_flag" (not an auto type), so this never recurses.
            cats = sorted(redacted_categories)
            self.flag(
                f"redacted {', '.join(cats)} from {etype}",
                action="redacted",
                severity="info",
                data=cats,
                auto=True,
            )
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
        action: FlagAction = "flagged",
        severity: FlagSeverity = "warning",
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
        covered = sorted({c for e in self.entries for c in controls.get(e.type, [])})
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
                    row["controls"] = controls.get(entry.type, [])
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
        action: FlagAction = "flagged",
        severity: FlagSeverity = "warning",
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
