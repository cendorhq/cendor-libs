"""``guard()`` — a batteries-included enforcement callable for ``cendor.core``'s interceptor seam.

The recorder/enforcer split stays intact: ``acttrace`` still only *records*. ``guard()`` returns a
plain callable you install yourself via ``core.add_interceptor`` — it is ``core`` that stops the
call, not ``acttrace``. Per call, the active :class:`~cendor.acttrace.Policy` resolves each detected
category to an action:

* **block** → record ``policy_flag(action="blocked")`` → **raise** (the call never runs).
* **redact** → scrub the outbound messages (via ``core``'s ``Reroute(messages=…)``) so the
  *provider* receives the cleaned content, record ``action="redacted"`` → proceed. Tools have no
  message-rewrite seam, so a redact on tool arguments stays record-only (block is the pre-send
  control there).
* **flag** → record ``policy_flag(action="flagged")`` → proceed untouched.
* nothing → proceed untouched.

Everything is offline and deterministic — the guard reuses the same :func:`~cendor.acttrace.scan`
engine, so there is no second detection path to keep in sync.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from cendor.core.instrument import MISS, Reroute
from cendor.core.types import LLMCall, ToolCall

from .detectors import _scrub
from .policy import Finding, Policy, scan

if TYPE_CHECKING:  # avoid a runtime import cycle (__init__ imports this module)
    from . import AuditLog

#: Severity ordering, so a grouped flag carries the strongest severity in the group.
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


class PolicyViolation(Exception):
    """Raised by a :func:`guard` to block an outbound call whose content a policy forbids.

    Carries the offending :class:`~cendor.acttrace.Finding` list on :attr:`findings` (categories and
    counts only — never the raw values).
    """

    def __init__(self, message: str = "policy violation", findings: list[Finding] | None = None):
        super().__init__(message)
        self.findings = findings or []


def _content(call: Any) -> Any:
    """The caller-supplied content of a call to scan (messages for LLMs, arguments for tools)."""
    if isinstance(call, LLMCall):
        return call.messages
    if isinstance(call, ToolCall):
        return call.arguments
    return None


def _max_severity(findings: list[Finding]) -> str:
    return max(
        (f.severity for f in findings), key=lambda s: _SEVERITY_RANK.get(s, 0), default="warning"
    )


def _make_block_exception(
    on_block: type[BaseException] | Callable[[list[Finding]], BaseException],
    findings: list[Finding],
) -> BaseException:
    cats = sorted({f.category for f in findings})
    message = f"policy blocked outbound call: {', '.join(cats)}"
    if isinstance(on_block, type):  # an exception class → construct with a message
        exc: BaseException = on_block(message)
        try:  # attach findings when the exception type tolerates it (e.g. PolicyViolation)
            exc.findings = findings  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass
        return exc
    return on_block(findings)  # a factory: list[Finding] -> Exception


def guard(
    policy: Policy | None = None,
    audit: AuditLog | None = None,
    on_block: type[BaseException] | Callable[[list[Finding]], BaseException] = PolicyViolation,
) -> Callable[[Any], Any]:
    """Return a pre-call interceptor that enforces ``policy`` and records refusals via ``audit``.

    Install it on ``core``'s seam::

        from cendor.core.instrument import add_interceptor
        from cendor.acttrace import AuditLog, Policy, guard

        log = AuditLog(system="support_bot", risk_tier="high")
        add_interceptor(guard(Policy.gdpr(), audit=log))   # enforce + record in one line

    Args:
        policy: The posture to enforce (defaults to :meth:`Policy.default`). Note that
            ``Policy.default()`` never blocks — use ``Policy.gdpr()`` / ``pci()`` / ``strict()``
            (or a custom policy) to make a category ``block``.
        audit: An :class:`~cendor.acttrace.AuditLog` to record ``policy_flag`` events on. Optional —
            without it the guard still enforces (blocks/proceeds), just silently.
        on_block: The exception to raise on a ``block`` hit — an exception **class** (called with
            a message) or a factory ``list[Finding] -> Exception``. Defaults to
            :class:`PolicyViolation`.

    Returns:
        A callable for :func:`cendor.core.instrument.add_interceptor`.
    """
    policy = policy or Policy.default()

    def _record(action: str, findings: list[Finding], call: Any, note: str = "") -> None:
        if audit is None:
            return
        cats = sorted({f.category for f in findings})
        kind = "llm_call" if isinstance(call, LLMCall) else "tool_call"
        reason = f"{action} {', '.join(cats)} in outbound {kind}"
        if note:
            reason = f"{reason} ({note})"
        severity = "info" if action == "redacted" else _max_severity(findings)
        audit.flag(reason, action=action, severity=severity, data=cats, auto=True)

    def _interceptor(call: Any) -> Any:
        content = _content(call)
        if content is None:
            return MISS
        findings = scan(content, policy)
        if not findings:
            return MISS
        blocked = [f for f in findings if f.action == "block"]
        to_redact = [f for f in findings if f.action == "redact"]
        flagged = [f for f in findings if f.action == "flag"]
        if blocked:
            _record("blocked", blocked, call)  # record the refusal *before* raising
            raise _make_block_exception(on_block, blocked)
        if flagged:
            _record("flagged", flagged, call)
        if to_redact:
            if isinstance(call, LLMCall):
                # Redact-before-send: scrub the outbound messages and reroute so the *provider*
                # receives the cleaned content, then record that we did. Reroute updates
                # call.messages too, keeping the emitted event consistent with what was sent.
                cleaned = _scrub(call.messages, {f.category for f in to_redact})
                _record("redacted", to_redact, call)
                return Reroute(messages=cleaned)
            # Tools have no message-rewrite seam (Reroute is for model calls), so a redact on tool
            # arguments stays record-only and the call proceeds — block is the pre-send control.
            _record(
                "flagged",
                to_redact,
                call,
                note="redact-before-send applies to model calls; tool arguments unchanged",
            )
        return MISS

    return _interceptor
