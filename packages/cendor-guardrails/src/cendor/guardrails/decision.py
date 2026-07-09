"""The guardrail abstraction and its evidence type. docs/guardrails.md §Core concepts.

This is the leaf module: it defines the *types* (``Verdict``, ``Context``, ``Guardrail``, the
``guardrail`` decorator) and the *evidence* a decision leaves behind (``GuardrailDecision`` — the
bus event acttrace chains — and ``GuardrailTripped`` — the fail-closed exception). It imports
nothing from the rest of the package, so ``rules`` and the engine in ``__init__`` can both build on
it without a cycle.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: The four intervention points, in agent-loop order. Mirrors Azure Foundry's intervention points
#: and OpenAI's four decorator types: gate the user turn (``input``), the model's request to call a
#: tool (``tool_call``), the tool's result before the model sees it (``tool_output``), and the
#: model's final answer (``output``).
STAGES: tuple[str, ...] = ("input", "tool_call", "tool_output", "output")

#: What a tripped check does. Mirrors acttrace's action vocabulary so a guardrail decision and a
#: policy flag read the same in an audit chain. (``"rewrite"`` is spelled ``"redact"`` — one verb
#: for "replace the payload and continue".)
ACTIONS: tuple[str, ...] = ("block", "redact", "flag")


def normalize_stages(stage: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Coerce a stage spec (a single stage or a collection) to a validated tuple."""
    stages = (stage,) if isinstance(stage, str) else tuple(stage)
    if not stages:
        raise ValueError("a guardrail must apply to at least one stage")
    for s in stages:
        if s not in STAGES:
            raise ValueError(f"unknown stage {s!r}; must be one of {STAGES}")
    return stages


@dataclass(frozen=True)
class Verdict:
    """What a check returns to *trip* a guardrail. Returning ``None`` (see :data:`ALLOW`) passes.

    Args:
        action: ``"block"`` (fail-closed — raise :class:`GuardrailTripped`), ``"redact"`` (replace
            the payload with :attr:`replacement` and continue), or ``"flag"`` (record and continue).
        reason: A short, human-readable explanation recorded on the decision. Keep it free of raw
            secret values — acttrace scrubs payloads, but a terse reason is better evidence anyway.
        replacement: The cleaned payload (messages/text) to substitute when ``action="redact"``.
    """

    action: str
    reason: str = ""
    replacement: Any = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}; must be one of {ACTIONS}")


#: Readable alias for "this check passed" — a check returns ``ALLOW`` (i.e. ``None``) to pass.
ALLOW: None = None


@dataclass
class Context:
    """Everything a check knows about *where* it is running, beyond the payload itself.

    Populated by the caller (the SDK runner, the ``install()`` interceptor, or a direct
    :func:`~cendor.guardrails.apply`). Every field is optional, so a standalone check can ignore it.
    """

    stage: str
    agent: str = ""
    tool: str = ""
    tool_args: Any = None
    trace_id: str = ""
    metadata: dict = field(default_factory=dict)


#: A check: given the payload and its :class:`Context`, return a :class:`Verdict` to trip or
#: ``None`` to pass. May be sync or ``async`` (an async check returns an awaitable and needs the
#: async evaluation path — :func:`~cendor.guardrails.evaluate_async`).
Check = Callable[[Any, Context], "Verdict | None | Awaitable[Verdict | None]"]


@dataclass
class Guardrail:
    """A named check bound to one or more stages. Build one directly, with the :func:`guardrail`
    decorator, or via a factory in :mod:`cendor.guardrails.rules`."""

    name: str
    stages: tuple[str, ...]
    check: Check

    def __post_init__(self) -> None:
        self.stages = normalize_stages(self.stages)


def guardrail(
    stage: str | tuple[str, ...] | list[str] = "input",
    name: str | None = None,
) -> Callable[[Check], Guardrail] | Guardrail:
    """Decorator sugar: turn a ``check(payload, ctx)`` function into a :class:`Guardrail`.

    ```python
    @guardrail(stage="input")
    def no_ssn(payload, ctx):
        if "ssn" in str(payload).lower():
            return Verdict("block", reason="SSN mentioned")
    ```

    Usable bare (``@guardrail``) — defaults to the ``input`` stage — or called
    (``@guardrail(stage="output", name="…")``).
    """
    if callable(stage):  # used bare as @guardrail
        fn = stage
        return Guardrail(name=name or _fn_name(fn), stages=("input",), check=fn)

    stages = normalize_stages(stage)

    def deco(fn: Check) -> Guardrail:
        return Guardrail(name=name or _fn_name(fn), stages=stages, check=fn)

    return deco


def _fn_name(fn: Any) -> str:
    return str(getattr(fn, "__name__", "guardrail"))


@dataclass
class GuardrailDecision:
    """Evidence that a guardrail tripped or flagged — emitted on the ``cendor.core`` bus.

    acttrace duck-types this (``guardrail``/``stage``/``action`` present) and chains it as a
    tamper-evident ``guardrail_decision`` entry. It carries *no raw payload* — only the guardrail's
    name, the resolved action, and a short reason — so the audit trail records the enforcement, not
    the content it acted on.
    """

    guardrail: str
    stage: str
    action: str
    reason: str = ""
    agent: str = ""
    tool: str = ""
    trace_id: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = field(default_factory=dict)


class GuardrailTripped(Exception):
    """Raised when a guardrail's action is ``block`` (fail-closed). Carries the decisions recorded
    up to and including the block on :attr:`decisions`."""

    def __init__(self, decisions: list[GuardrailDecision]):
        self.decisions = decisions
        blocking = next((d for d in decisions if d.action == "block"), None)
        if blocking is not None:
            msg = f"guardrail {blocking.guardrail!r} blocked at stage {blocking.stage!r}"
            if blocking.reason:
                msg = f"{msg}: {blocking.reason}"
        else:  # defensive — GuardrailTripped is only raised with a blocking decision
            msg = "guardrail blocked the call"
        super().__init__(msg)
