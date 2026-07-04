"""Policy resolution and the pure ``scan`` / ``redact`` surface for ``cendor.acttrace``.

A :class:`Policy` maps a detected category (or its group) to an **action** —
``allow`` · ``flag`` · ``redact`` · ``block`` — so the same detection engine can serve very
different postures. :func:`scan` reports what's present (counts only, never raw values);
:func:`redact` returns a scrubbed copy plus those findings. Neither touches the audit chain or the
network — enforcement (block / redact-before-send) is a separate, opt-in concern (see ``guard``).

The presets encode the roadmap's recommended postures:

* :meth:`Policy.default` — today's behaviour: secrets & email ``redact``, everything else ``flag``.
* :meth:`Policy.gdpr` — special-category ``block``; other personal data ``redact``.
* :meth:`Policy.pci` — payment/financial data ``block``.
* :meth:`Policy.strict` — high-severity groups ``block``, the rest ``redact``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .detectors import _scan_counts, _scrub

#: The allowed policy actions, from most permissive to most severe.
ACTIONS = ("allow", "flag", "redact", "block")

#: Actions that cause :func:`redact` (and the built-in ``AuditLog`` path) to scrub the value from
#: the record. ``block`` is scrubbed too, for record safety — the raw value should never be chained.
_SCRUB_ACTIONS = frozenset({"redact", "block"})


class Policy:
    """Maps each detected category → an action, with a fallthrough ``default``.

    Keys in ``actions`` may be a specific **category** (``"credit_card"``) or a **group**
    (``"financial"``); a category-specific entry wins over its group, which wins over ``default``.

    A plain class (not a dataclass) so the ``default()`` preset constructor and the fallthrough
    action can share the name space cleanly. The fallthrough is set via the ``default=`` constructor
    argument and read through :meth:`action_for` / :attr:`default_action` (a plain ``default``
    attribute would collide with the :meth:`default` classmethod).
    """

    def __init__(self, actions: dict[str, str] | None = None, default: str = "flag") -> None:
        self.actions: dict[str, str] = dict(actions or {})
        self._default: str = default

    @property
    def default_action(self) -> str:
        """The fallthrough action for a category not named (by category or group) in ``actions``."""
        return self._default

    def __repr__(self) -> str:
        return f"Policy(actions={self.actions!r}, default={self._default!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Policy)
            and other.actions == self.actions
            and other._default == self._default
        )

    def action_for(self, category: str, group: str = "") -> str:
        """Resolve the action for a category (most specific wins: category → group → default)."""
        if category in self.actions:
            return self.actions[category]
        if group and group in self.actions:
            return self.actions[group]
        return self._default

    @classmethod
    def default(cls) -> Policy:
        """Today's behaviour: secrets and email are ``redact``ed, everything else is ``flag``ged."""
        return cls(actions={"secret": "redact", "email": "redact"}, default="flag")

    @classmethod
    def gdpr(cls) -> Policy:
        """GDPR-leaning: special-category data ``block``, other personal/secret data ``redact``."""
        return cls(
            actions={
                "special_category": "block",
                "pii": "redact",
                "gov_id": "redact",
                "financial": "redact",
                "secret": "redact",
                "credential": "block",
            },
            default="flag",
        )

    @classmethod
    def pci(cls) -> Policy:
        """PCI-leaning: payment/financial data ``block``; secrets & PII ``redact``."""
        return cls(
            actions={"financial": "block", "secret": "redact", "pii": "redact"},
            default="flag",
        )

    @classmethod
    def strict(cls) -> Policy:
        """Highest recall: high-severity groups ``block``, everything else ``redact``."""
        return cls(
            actions={
                "secret": "block",
                "credential": "block",
                "financial": "block",
                "gov_id": "block",
            },
            default="redact",
        )


@dataclass(frozen=True)
class Finding:
    """One category detected in a payload — a count and its resolved action, never the raw value."""

    category: str
    group: str
    severity: str
    action: str
    count: int


def scan(obj: Any, policy: Policy | None = None) -> list[Finding]:
    """Detect sensitive data in ``obj`` (str/dict/list) and resolve each category to an action.

    Returns one :class:`Finding` per detected category, sorted by category. Reports **counts
    only** — the raw offending values are never returned (so a caller can't accidentally log a
    secret). Uses :meth:`Policy.default` when ``policy`` is ``None``.
    """
    policy = policy or Policy.default()
    findings: list[Finding] = []
    for category, (det, count) in _scan_counts(obj).items():
        action = policy.action_for(det.category, det.group)
        findings.append(Finding(category, det.group, det.severity, action, count))
    findings.sort(key=lambda f: f.category)
    return findings


def redact(obj: Any, policy: Policy | None = None) -> tuple[Any, list[Finding]]:
    """Scrub ``obj`` per ``policy`` and return ``(cleaned_copy, findings)``.

    Only categories whose resolved action is ``redact`` or ``block`` are scrubbed (a ``block``
    value is removed for record safety even though *enforcing* the block is a separate step).
    ``flag``/``allow`` categories are reported in ``findings`` but left in place.
    """
    findings = scan(obj, policy)
    scrub = {f.category for f in findings if f.action in _SCRUB_ACTIONS}
    cleaned = _scrub(obj, scrub) if scrub else obj
    return cleaned, findings
