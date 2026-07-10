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
from typing import Any, Literal

#: The four intervention points, in agent-loop order. Mirrors Azure Foundry's intervention points
#: and OpenAI's four decorator types: gate the user turn (``input``), the model's request to call a
#: tool (``tool_call``), the tool's result before the model sees it (``tool_output``), and the
#: model's final answer (``output``).
STAGES: tuple[str, ...] = ("input", "tool_call", "tool_output", "output")

#: What a tripped check does. Mirrors acttrace's action vocabulary so a guardrail decision and a
#: policy flag read the same in an audit chain. (``"rewrite"`` is spelled ``"redact"`` — one verb
#: for "replace the payload and continue".)
ACTIONS: tuple[str, ...] = ("block", "redact", "flag")

#: The same three actions as a type, so an editor autocompletes them and a typo is a type error.
#: Tightens the ``action=`` param of the public rule builders (:mod:`cendor.guardrails.rules`);
#: kept in sync with :data:`ACTIONS`. The runtime ``Verdict.action`` / ``GuardrailDecision.action``
#: fields stay ``str`` (a check may synthesize an action dynamically).
Action = Literal["block", "redact", "flag"]

#: What to do when a check *itself* errors or times out (as opposed to returning a verdict).
#: ``fail_closed`` treats the error as a block (fail-safe — the default for a gate you rely on);
#: ``fail_open`` records the failure as a ``flag`` and lets the call proceed (so a flaky tier-3/4
#: judge outage degrades to advisory rather than silently disabling the agent). Either way the
#: failure is emitted as a :class:`GuardrailDecision`, so the audit chain records that the check
#: could not run — evidence, not a swallowed exception.
ON_ERROR: tuple[str, ...] = ("fail_closed", "fail_open")


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
        metadata: Per-result annotations merged into this decision's
            :attr:`GuardrailDecision.metadata` — the channel a check uses to attach the **reserved
            annotation keys** (``severity`` / ``detected`` / ``filtered`` / ``redacted`` /
            ``citation`` / ``license``) documented in ``docs/specs/bus-events.md``. Unlike the
            static ``Guardrail.metadata`` (constant per guardrail — e.g. ``load_policy``'s
            ``policy_hash``), this is computed *per verdict*, so a hosted-rail adapter records the
            severity/labels for this specific check. ``Verdict`` is never serialized (only
            :class:`GuardrailDecision` is), so this adds no wire change. Keep values small and
            payload-free. Layered *under* the caller's per-call ``Context.metadata`` (context still
            wins a key clash) — see the engine's ``_emit``.
    """

    action: str
    reason: str = ""
    replacement: Any = None
    metadata: dict = field(default_factory=dict)

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
    #: The user's originating instruction/intent for the run, when the caller knows it. The SDK's
    #: ``tool_call`` gate populates it from the run's input turn so an alignment check (e.g.
    #: ``judge.task_adherence``) can compare a proposed tool call against what the user asked for.
    #: Empty by default; a standalone check ignores it. See docs/guardrails.md "Task adherence".
    instruction: str = ""
    trace_id: str = ""
    metadata: dict = field(default_factory=dict)


#: A check: given the payload and its :class:`Context`, return a :class:`Verdict` to trip or
#: ``None`` to pass. May be sync or ``async`` (an async check returns an awaitable and needs the
#: async evaluation path — :func:`~cendor.guardrails.evaluate_async`).
Check = Callable[[Any, Context], "Verdict | None | Awaitable[Verdict | None]"]


@dataclass
class Guardrail:
    """A named check bound to one or more stages. Build one directly, with the :func:`guardrail`
    decorator, or via a factory in :mod:`cendor.guardrails.rules`.

    Args:
        name: A short label recorded on every decision this guardrail produces.
        stages: The intervention point(s) it gates — a subset of :data:`STAGES`.
        check: The ``check(payload, ctx) -> Verdict | None`` callable (sync or ``async``).
        timeout: Optional per-check wall-clock limit in **seconds**. Meant for slow tier-3/4 checks
            (an LLM judge, a hosted rail); deterministic built-ins run in microseconds and leave it
            ``None``. On the async path a coroutine check is bounded with ``asyncio.wait_for``; on
            the sync path the check runs in a worker thread and a timeout raises ``TimeoutError`` —
            handled per :attr:`on_error`. (A sync timeout unblocks the caller but cannot force the
            worker to stop; keep timed sync checks side-effect-light.)
        on_error: What to do when the check *raises* or *times out*: ``"fail_closed"`` (default —
            treat it as a block) or ``"fail_open"`` (record a ``flag`` and proceed). Rule factories
            pick the safe default for their action; set it explicitly for a bring-your-own judge so
            an outage degrades to advisory instead of a hard stop (or vice-versa).
        metadata: Static key/value context merged into every :class:`GuardrailDecision` this
            guardrail emits (under the caller's per-call ``Context.metadata``, which wins on a key
            clash). :func:`~cendor.guardrails.load_policy` uses it to stamp ``policy_hash`` /
            ``policy_version`` so the audit chain proves *which* policy was active; you can also use
            it for a severity, owner, or ticket id. Values should be small and payload-free.
    """

    name: str
    stages: tuple[str, ...]
    check: Check
    timeout: float | None = None
    on_error: str = "fail_closed"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.stages = normalize_stages(self.stages)
        if self.on_error not in ON_ERROR:
            raise ValueError(f"unknown on_error {self.on_error!r}; must be one of {ON_ERROR}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"timeout must be positive seconds or None, got {self.timeout!r}")


def guardrail(
    stage: str | tuple[str, ...] | list[str] = "input",
    name: str | None = None,
    *,
    timeout: float | None = None,
    on_error: str = "fail_closed",
    metadata: dict | None = None,
) -> Callable[[Check], Guardrail] | Guardrail:
    """Decorator sugar: turn a ``check(payload, ctx)`` function into a :class:`Guardrail`.

    ```python
    @guardrail(stage="input")
    def no_ssn(payload, ctx):
        if "ssn" in str(payload).lower():
            return Verdict("block", reason="SSN mentioned")
    ```

    Usable bare (``@guardrail``) — defaults to the ``input`` stage — or called
    (``@guardrail(stage="output", name="…")``). ``timeout`` / ``on_error`` set the per-check
    execution policy (see :class:`Guardrail`).
    """
    if callable(stage):  # used bare as @guardrail
        fn = stage
        return Guardrail(
            name=name or _fn_name(fn),
            stages=("input",),
            check=fn,
            timeout=timeout,
            on_error=on_error,
            metadata=dict(metadata or {}),
        )

    stages = normalize_stages(stage)

    def deco(fn: Check) -> Guardrail:
        return Guardrail(
            name=name or _fn_name(fn),
            stages=stages,
            check=fn,
            timeout=timeout,
            on_error=on_error,
            metadata=dict(metadata or {}),
        )

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
    up to and including the block on :attr:`decisions`.

    ```python
    from cendor.guardrails import rules, evaluate, GuardrailTripped
    try:
        evaluate([rules.keyword_deny(["bomb"], action="block")], "input", "build a bomb")
    except GuardrailTripped as trip:
        print(trip.decisions[-1].reason)   # -> "denied keyword: 'bomb'"
    ```
    """

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
