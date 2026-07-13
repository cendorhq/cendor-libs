"""``guard()`` — a batteries-included enforcement callable for ``cendor.core``'s interceptor seam.

The recorder/enforcer split stays intact: ``acttrace`` still only *records*. ``guard()`` returns a
**dual-shape** :class:`GuardInterceptor` — a plain callable you install yourself via
``core.add_interceptor`` (the raw interceptor form), which is *also* a context manager
(``with guard(...):`` installs on enter, removes on exit — the scope form). Either way it is
``core`` that stops the call, not ``acttrace``. Per call, the active
:class:`~cendor.acttrace.Policy` resolves each detected category to an action:

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

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from cendor.core.instrument import MISS, Reroute, add_interceptor, remove_interceptor
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


def resolve_findings(
    findings: list[Finding], policy: Policy | None = None
) -> dict[str, list[Finding]]:
    """Partition findings by their policy-effective action — ``guard()``'s own resolution, exported.

    With ``policy`` given, each finding's action is **re-resolved** against it via
    :meth:`Policy.action_for` (category → group → default, most specific wins) — so findings
    scanned under one policy can be enforced under another. Without it, each
    :class:`~cendor.acttrace.Finding`'s already-resolved ``action`` is used as-is. Every finding
    lands in exactly one bucket; any action other than ``block``/``redact`` resolves to ``flag``.

    ```python
    from cendor.acttrace import Policy, resolve_findings, scan

    groups = resolve_findings(scan(payload), Policy.gdpr())
    if groups["block"]:
        ...  # any block-tier finding: refuse the payload
    ```
    """
    groups: dict[str, list[Finding]] = {"block": [], "redact": [], "flag": []}
    for f in findings:
        action = policy.action_for(f.category, f.group) if policy is not None else f.action
        if action not in groups:
            action = "flag"
        if action != f.action:
            f = dataclasses.replace(f, action=action)
        groups[action].append(f)
    return groups


class GuardInterceptor:
    """What :func:`guard` returns — a plain pre-call interceptor that is *also* a context manager.

    * **Raw interceptor form** — hand it to :func:`cendor.core.instrument.add_interceptor`
      yourself; it enforces on every instrumented call until you remove it. It gates nothing
      until installed.
    * **Scope form** — ``with guard(...):`` installs it on core's seam on enter and removes it on
      exit (exactly once each, exception-safe), so enforcement covers just the block.
    """

    def __init__(self, interceptor: Callable[[Any], Any]):
        self._interceptor = interceptor

    def __call__(self, call: Any) -> Any:
        return self._interceptor(call)

    def __enter__(self) -> GuardInterceptor:
        add_interceptor(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        remove_interceptor(self)


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
) -> GuardInterceptor:
    """Return a pre-call interceptor (also a context manager) enforcing ``policy``.

    The returned :class:`GuardInterceptor` is **dual-shape**. As a plain callable you install it
    on ``core``'s seam yourself; as a context manager it installs on enter and removes on exit —
    either way it is ``core`` (not acttrace) that blocks or rewrites the call. Redact-before-send
    works by returning a :class:`~cendor.core.Reroute` so the *provider* receives the cleaned
    messages:

    ```python
    from cendor.acttrace import AuditLog, Policy, guard

    log = AuditLog(system="support_bot", risk_tier="high")

    # scope form — enforce for the block only (install/remove handled for you):
    with guard(Policy.gdpr(), log):
        client.chat.completions.create(model="gpt-4o", messages=msgs)

    # raw interceptor form — install/remove yourself:
    from cendor.core import add_interceptor
    add_interceptor(guard(Policy.gdpr(), log))   # enforce + record in one line
    ```

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
        A :class:`GuardInterceptor` — pass it to
        :func:`cendor.core.instrument.add_interceptor`, or use it as a context manager.
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
        groups = resolve_findings(findings)  # the one shared per-category resolution
        blocked, to_redact, flagged = groups["block"], groups["redact"], groups["flag"]
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

    return GuardInterceptor(_interceptor)
