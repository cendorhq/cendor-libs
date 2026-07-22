"""cendor.tokenguard — pre-flight cost caps + free spend attribution. See docs/tokenguard.md.

It **subscribes** to ``cendor.core``'s event bus and never patches a client itself (the
locked architecture: one ``instrument()``, many subscribers). Once a client is instrumented,
``@budget`` enforces a cap and ``track(...)`` attributes spend by tags — with zero per-call wiring.

Enforcement model (v0): every instrumented call emits an ``LLMCall`` on the bus *after* it
returns; tokenguard's subscriber records the spend and, if it pushes an active budget over its
cap, trips the breaker *synchronously inside that call* — so a runaway loop stops before the
next call runs. :func:`estimate` provides true pre-flight projection you can check proactively.
"""

from __future__ import annotations

import functools
import inspect
import threading
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from cendor.core import add_ambient_provider, bus, prices, tokens
from cendor.core.instrument import MISS, Reroute, add_interceptor
from cendor.core.types import LLMCall, Money

__all__ = [
    "budget",
    "track",
    "estimate",
    "report",
    "downgrades",
    "clamps",
    "use_sink",
    "configure",
    "dropped",
    "unpriced_calls",
    "Report",
    "BudgetEvent",
    "BudgetExceeded",
    "UnpricedModelWarning",
    "reset",
]


@dataclass
class BudgetEvent:
    """A pre-flight budget action, emitted on the ``cendor.core`` bus so ``acttrace`` chains it as a
    ``budget_event`` and an OpenTelemetry mirror can surface it in your APM/SIEM. A blocked call
    never reaches the bus as an ``LLMCall`` (it's refused pre-flight), so this event is the *only*
    signal that the breaker fired — which is exactly the governance action you want to alert on.

    ``action`` is ``"blocked"`` | ``"downgraded"`` | ``"clamped"``. Money fields are the ``Decimal``
    rendered as a string (never a float); token fields are ints. Duck-typed by ``acttrace`` (no
    import), mirroring how ``guardrails`` emits its ``GuardrailDecision``.
    """

    action: str
    reason: str = ""
    name: str | None = None  # the budget's human name (budget(name=...)), for UI/alert grouping
    description: str | None = None  # a longer human description of what the budget guards
    model: str = ""
    to_model: str | None = None  # the cheaper model, for action="downgraded"
    scope: str | None = None
    projected_usd: str | None = None
    cap_usd: str | None = None
    projected_tokens: int | None = None
    cap_tokens: int | None = None
    tags: dict = field(default_factory=dict)
    #: The run/trace id of the call this action guarded (GLR-6; from ``call.trace_id``, in hand at
    #: emit). ``""`` when the call carried none. The only field linking a ``budget_event`` to its
    #: run — ``acttrace`` copies it into the audit entry's ``run_id`` for the monitor's join.
    trace_id: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


#: Output tokens are unknown pre-flight; reserve this many for the downgrade projection unless
#: the request carries an explicit ``max_tokens``. docs/tokenguard.md §7.
_DEFAULT_OUTPUT_RESERVE = 256


class BudgetExceeded(Exception):
    """Raised when a call pushes an active ``on_exceed="raise"`` budget over its cap."""


class UnpricedModelWarning(UserWarning):
    """Warned (once per model) when a USD budget is active but the call's model has no price.

    An unpriced model records ``$0`` toward USD spend, so a USD-only cap can't enforce against it.
    Filter or escalate it like any warning (``simplefilter("error", UnpricedModelWarning)``); or set
    :func:`configure` ``on_unpriced="raise"`` to make ``on_exceed="block"`` reject such calls.
    """


class _Truncated(Exception):
    """Internal graceful-degradation signal for ``on_exceed="truncate"``; never escapes a budget."""


@dataclass
class _Frame:
    cap_usd: Decimal | None
    cap_tokens: int | None
    on_exceed: OnExceedMode | Callable[[dict], Any]
    scope: str | None = None
    downgrade: dict | None = None  # model -> cheaper model, for on_exceed="downgrade"
    output_reserve: int = _DEFAULT_OUTPUT_RESERVE  # pre-flight output projection (block/downgrade)
    reasoning_reserve: int = 0  # extra output headroom for a reasoning model's hidden thinking
    name: str | None = None  # budget identity, carried onto BudgetEvent (budget(name=...))
    description: str | None = None  # longer human description of what this budget guards
    spent_usd: Decimal = Decimal("0")
    spent_tokens: int = 0
    calls: int = 0


#: Valid string values for ``budget(on_exceed=...)`` (a callable is also accepted).
_ON_EXCEED = ("raise", "block", "truncate", "downgrade", "clamp")

#: The five overflow strategies, as a type so an editor autocompletes them and a typo is a type
#: error (a ``Callable[[dict], Any]`` is also accepted). Keep in sync with :data:`_ON_EXCEED`.
OnExceedMode = Literal["raise", "block", "truncate", "downgrade", "clamp"]

#: Per-provider request kwarg that caps generated (reasoning + visible) output tokens, used by
#: ``on_exceed="clamp"``. On these providers the cap is enforced server-side and *includes*
#: reasoning tokens, so injecting it bounds a reasoning model's real spend. Providers absent here
#: put the cap in nested config, so clamp can't inject it safely and falls back to a block.
_CLAMP_KWARG = {"openai": "max_completion_tokens", "anthropic": "max_tokens"}


@dataclass
class _Record:
    tags: dict
    usd: Decimal
    input_tokens: int
    output_tokens: int
    model: str
    reasoning_tokens: int = 0
    calls: int = 1
    unpriced: bool = False  # True when the call had no cost (unknown model) — a USD blind spot


_tags: ContextVar[dict | None] = ContextVar("cendor_tokenguard_tags", default=None)
_budgets: ContextVar[tuple[_Frame, ...]] = ContextVar("cendor_tokenguard_budgets", default=())
#: GLR-5 (Bug A): reserved internal metadata key holding the frame tuple + tags captured **at call
#: initiation** (the ``_pre`` frame, via the ambient seam), so ``_on_call`` can enforce/accrue/
#: attribute even when it fires **outside the originating scope** — the streamed-call case (and, in
#: Python, a consumer call after a stream generator leaked+restored scopes). Frames ride **by
#: reference** (the same mutable ``_Frame`` objects the scope's ``Handle``/report reads), so accrual
#: mutates the shared objects — no forked accounting. A metadata key (not a ``WeakKeyDictionary``:
#: a non-frozen ``LLMCall`` is unhashable); it never leaves the process (no serializer reads it).
_TG_ATTACH_KEY = "_cendor_tokenguard_attach"
_records: list[_Record] = []
_records_lock = threading.Lock()  # guards _records + _dropped (non-atomic read-modify-write)
_downgrades: list[dict] = []  # {"from", "to", "tags"} per pre-flight reroute
_clamps: list[dict] = []  # {"model", "kwarg", "limit", "tags"} per pre-flight token clamp
_sink: Any = None  # optional cendor.core.protocols.Sink to also persist each spend row

#: Cap on the in-memory spend buffer so a long-running process can't grow it without bound. When
#: exceeded, the oldest rows are evicted FIFO and counted by :func:`dropped`; :func:`report` then
#: reflects only the retained window. ``None`` disables the cap. For durable, complete history, use
#: a sink (:func:`use_sink`) and/or :func:`reset` between units of work. docs/tokenguard.md §5.
_DEFAULT_MAX_RECORDS = 100_000
_max_records: int | None = _DEFAULT_MAX_RECORDS
_dropped: int = 0  # spend rows evicted by the cap since the last reset()

#: Sentinel so a partial ``configure(...)`` call leaves the other settings untouched.
_UNSET: Any = object()

#: How a USD budget treats a call whose model has no price: ``"warn"`` (default — warn once per
#: model and let the call proceed, recording $0) or ``"raise"`` (``on_exceed="block"`` rejects the
#: unpriced call pre-flight with :class:`BudgetExceeded`). docs/tokenguard.md.
_DEFAULT_ON_UNPRICED = "warn"
_on_unpriced: str = _DEFAULT_ON_UNPRICED
_warned_unpriced: set[str] = set()  # models already warned about (warn once per model, per reset)


def _warn_unpriced(model: str, mode: str) -> None:
    """Warn once per model that an active USD budget can't enforce against an unpriced model."""
    if model in _warned_unpriced:
        return
    _warned_unpriced.add(model)
    warnings.warn(
        f"tokenguard: no price for model {model!r}, so the active USD budget "
        f"(on_exceed={mode!r}) counts its calls as $0 and cannot enforce a USD cap on it. "
        f"Add a rate (cendor.core.prices), use a tokens= cap instead, or "
        f"configure(on_unpriced='raise') to reject unpriced calls under on_exceed='block'.",
        UnpricedModelWarning,
        stacklevel=3,
    )


def use_sink(sink: Any) -> Any:
    """Attach a spend sink (e.g. ``sinks.SQLiteSink``/``sinks.OTelSink``); returns the previous one.

    The in-memory aggregation (:func:`report`) always runs; a sink *additionally* persists each
    row. Pass ``None`` to detach. docs/tokenguard.md §5.
    """
    global _sink
    previous, _sink = _sink, sink
    return previous


def configure(*, max_records: int | None = _UNSET, on_unpriced: str = _UNSET) -> None:
    """Tune tokenguard's runtime behavior. Each argument is independent — omit one to leave it as
    is. docs/tokenguard.md §5.

    Args:
        max_records: Caps how many spend rows are retained; once exceeded, the oldest are evicted
            FIFO (counted by :func:`dropped`). ``None`` makes the buffer unbounded (the pre-0.6
            behavior) — fine for short scripts and tests, but a long-running process should keep a
            cap and persist durable history via a sink (:func:`use_sink`). Defaults to 100k rows.
        on_unpriced: How a USD budget handles a call whose model has no price (its cost is ``None``,
            so it records ``$0`` and a USD cap can't bite). ``"warn"`` (default) emits an
            :class:`UnpricedModelWarning` once per model and lets the call proceed; ``"raise"``
            makes ``on_exceed="block"`` reject the unpriced call pre-flight with
            :class:`BudgetExceeded` (a strict cap that refuses what it can't price). Either way,
            unpriced calls are counted by :func:`unpriced_calls` and surfaced per-row by
            :func:`report`.
    """
    global _max_records, _on_unpriced
    if max_records is not _UNSET:
        _max_records = max_records
    if on_unpriced is not _UNSET:
        if on_unpriced not in ("warn", "raise"):
            raise ValueError(f"on_unpriced must be 'warn' or 'raise', got {on_unpriced!r}")
        _on_unpriced = on_unpriced


def dropped() -> int:
    """Spend rows evicted by the :func:`configure` cap since the last :func:`reset` (0 if none)."""
    return _dropped


def unpriced_calls() -> int:
    """Count of recorded calls whose cost was ``None`` (unpriced/unknown model) in the retained
    buffer — a blind spot for any USD budget, since they contribute ``$0`` to USD spend."""
    with _records_lock:
        return sum(rec.calls for rec in _records if rec.unpriced)


def _current_tags() -> dict:
    return _tags.get() or {}


def _tokenguard_ambient(event: Any) -> dict | None:
    """The ambient provider (GLR-5): at every event's construction — the caller's synchronous frame,
    where the budget/track scopes are unconditionally correct — snapshot the live frame tuple (by
    reference) and the current tags into a reserved metadata key. ``_on_call`` reads this back at
    delivery time. Attaches only for ``LLMCall``s and only when a scope is active. Never raises."""
    if not isinstance(event, LLMCall):
        return None
    frames = _budgets.get()
    tags = _tags.get()
    if frames or tags is not None:
        return {_TG_ATTACH_KEY: (frames, dict(tags) if tags else {})}
    return None


def _ensure_subscribed() -> None:
    bus.subscribe(_on_call)  # idempotent on the bus side
    add_interceptor(_preflight_interceptor)  # idempotent; pre-flight downgrade/block routing
    add_ambient_provider(_tokenguard_ambient)  # idempotent; captures frames/tags pre-emit (GLR-5)


def _projected_output(call: LLMCall, reserve: int, reasoning_reserve: int = 0) -> int:
    """Output tokens to assume pre-flight.

    Prefers the request's explicit output cap — ``max_completion_tokens`` (what OpenAI reasoning
    models use) or ``max_tokens`` — which on OpenAI/Anthropic *already includes* reasoning tokens,
    so it's a correct upper bound with no reasoning add-on. When neither is set, output is unknown:
    assume ``reserve`` plus ``reasoning_reserve`` (extra headroom for a reasoning model's hidden
    thinking, which can't be predicted pre-flight — see docs/tokenguard.md §7).
    """
    kwargs = call.metadata.get("request_kwargs") or {}
    # Explicit `is not None` checks: max_tokens=0 is a real cap (project 0 output), not "unset".
    explicit = kwargs.get("max_completion_tokens")
    if explicit is None:
        explicit = kwargs.get("max_tokens")
    if explicit is not None:
        return int(explicit)
    return reserve + reasoning_reserve


def _estimate_event(
    call: LLMCall, reserve: int = _DEFAULT_OUTPUT_RESERVE, reasoning_reserve: int = 0
) -> Decimal:
    """Project a call's cost pre-flight from its model + messages (+ an output reserve)."""
    input_tokens = tokens.count(call.messages, call.model)
    projected = _projected_output(call, reserve, reasoning_reserve)
    return prices.estimate(call.model, input_tokens, projected).amount


def _project_tokens(
    call: LLMCall, reserve: int = _DEFAULT_OUTPUT_RESERVE, reasoning_reserve: int = 0
) -> int:
    """Pre-flight token projection: input tokens + the output reserve (max_tokens or default)."""
    return tokens.count(call.messages, call.model) + _projected_output(
        call, reserve, reasoning_reserve
    )


# --- G15: native governance counter (optional, no-op without OpenTelemetry) ---
#: Lazily-created ``cendor.tokenguard.budget.events`` counter (meter ``cendor.tokenguard`` — the
#: same meter ``OTelSink`` uses). ``None`` until first use; stays ``None`` if OTel isn't installed.
_budget_events_counter: Any = None
_budget_events_counter_checked = False


def _budget_events_add(attrs: dict[str, Any]) -> None:
    """Increment the ``cendor.tokenguard.budget.events`` counter (no-op without OpenTelemetry).

    Renders as ``cendor_tokenguard_budget_events_total`` in Prometheus. A lazily-created counter on
    a proxy meter binds to whatever ``MeterProvider`` the host app configures (before or after first
    use). Best-effort observability — it never gates the budget action.
    """
    global _budget_events_counter, _budget_events_counter_checked
    if not _budget_events_counter_checked:
        _budget_events_counter_checked = True
        try:
            from opentelemetry import metrics
        except ImportError:
            _budget_events_counter = None
        else:
            _budget_events_counter = metrics.get_meter("cendor.tokenguard").create_counter(
                "cendor.tokenguard.budget.events"
            )
    if _budget_events_counter is not None:
        _budget_events_counter.add(1, attrs)


def _emit_budget_event(
    action: str,
    *,
    call: LLMCall,
    frame: _Frame,
    reason: str,
    projected_usd: Decimal | None = None,
    projected_tokens: int | None = None,
    to_model: str | None = None,
) -> None:
    """Publish a :class:`BudgetEvent` on the bus for a pre-flight budget action, so ``acttrace``
    records it and an OTel mirror can alert on it. Best-effort observability — never gates the
    action itself (the caller still raises/reroutes)."""
    bus.emit(
        BudgetEvent(
            action=action,
            reason=reason,
            name=frame.name,
            description=frame.description,
            model=call.model,
            to_model=to_model,
            scope=frame.scope,
            projected_usd=str(projected_usd) if projected_usd is not None else None,
            cap_usd=str(frame.cap_usd) if frame.cap_usd is not None else None,
            projected_tokens=projected_tokens,
            cap_tokens=frame.cap_tokens,
            tags=dict(_current_tags()),
            trace_id=call.trace_id,  # GLR-6 linkage: the emitter has the call's trace id in hand
        )
    )
    # G15: optional native governance counter (no-op without OpenTelemetry). Bounded label set —
    # `name` must be a fixed identifier (docstring warns) so the time-series count stays bounded.
    counter_attrs: dict[str, Any] = {"action": action, "model": call.model}
    if frame.scope:
        counter_attrs["scope"] = frame.scope
    if frame.name:
        counter_attrs["name"] = frame.name
    _budget_events_add(counter_attrs)


def _preflight_interceptor(call: object) -> Any:
    """Pre-flight enforcement, before the call runs: reroute (``downgrade``), clamp (``clamp``), or
    block (``block``).

    ``downgrade`` reroutes to a cheaper model when the projection would breach the cap; ``clamp``
    injects a provider output ceiling so the call can't exceed the remaining token budget; ``block``
    raises :class:`BudgetExceeded` *before* the call runs, so the over-budget call never executes
    (a true circuit breaker). ``raise``/``truncate`` stay post-flight (enforced after the call).
    """
    if not isinstance(call, LLMCall):
        return MISS
    for frame in reversed(_budgets.get()):  # innermost-first
        if frame.on_exceed == "downgrade" and frame.downgrade and frame.cap_usd is not None:
            cheaper = frame.downgrade.get(call.model)
            if not cheaper:
                continue
            try:
                projected = _estimate_event(call, frame.output_reserve, frame.reasoning_reserve)
            except KeyError:
                _warn_unpriced(call.model, "downgrade")  # can't project — no longer silent
                continue  # unknown model price — leave the call as-is
            if frame.spent_usd + projected > frame.cap_usd:
                _downgrades.append(
                    {"from": call.model, "to": cheaper, "tags": dict(_current_tags())}
                )
                _emit_budget_event(
                    "downgraded",
                    call=call,
                    frame=frame,
                    reason=f"projected ${frame.spent_usd + projected} > cap ${frame.cap_usd}; "
                    f"rerouted {call.model} -> {cheaper}",
                    projected_usd=frame.spent_usd + projected,
                    to_model=cheaper,
                )
                return Reroute(model=cheaper)
        elif frame.on_exceed == "clamp":
            reroute = _clamp(call, frame)
            if reroute is not None:
                return reroute
        elif frame.on_exceed == "block":
            proj_tokens = _project_tokens(call, frame.output_reserve, frame.reasoning_reserve)
            if frame.cap_tokens is not None and frame.spent_tokens + proj_tokens > frame.cap_tokens:
                reason = (
                    f"pre-flight block: ~{frame.spent_tokens + proj_tokens} tokens would exceed "
                    f"cap {frame.cap_tokens} (model={call.model})"
                )
                _emit_budget_event(
                    "blocked",
                    call=call,
                    frame=frame,
                    reason=reason,
                    projected_tokens=frame.spent_tokens + proj_tokens,
                )
                raise BudgetExceeded(reason)
            if frame.cap_usd is not None:
                try:
                    projected = _estimate_event(call, frame.output_reserve, frame.reasoning_reserve)
                except KeyError:
                    if _on_unpriced == "raise":
                        reason = (  # strict: reject what we can't price (loud enough)
                            f"pre-flight block: model={call.model} has no price, so a USD cap "
                            f"cannot be projected; configure(on_unpriced='raise') rejects unpriced "
                            f"calls (set on_unpriced='warn' to let them through as $0)."
                        )
                        _emit_budget_event("blocked", call=call, frame=frame, reason=reason)
                        raise BudgetExceeded(reason) from None
                    _warn_unpriced(call.model, "block")
                    continue
                if frame.spent_usd + projected > frame.cap_usd:
                    reason = (
                        f"pre-flight block: projected ${frame.spent_usd + projected} would exceed "
                        f"cap ${frame.cap_usd} (model={call.model})"
                    )
                    _emit_budget_event(
                        "blocked",
                        call=call,
                        frame=frame,
                        reason=reason,
                        projected_usd=frame.spent_usd + projected,
                    )
                    raise BudgetExceeded(reason)
    return MISS


def _clamp(call: LLMCall, frame: _Frame) -> Reroute | None:
    """Inject a provider output ceiling so a single call can't exceed the remaining token budget.

    You can't *predict* a reasoning model's hidden thinking pre-flight, so the only way to bound it
    is to hand the provider its own cap (``max_completion_tokens`` / ``max_tokens``, which include
    reasoning) and let it stop generation server-side. Requires a ``tokens=`` cap. It **always**
    injects the ceiling — set to the tokens left in the budget after the projected input — so even a
    call that *looks* small pre-flight can't overshoot on a surprise-long completion (the reserve
    heuristic only guards ``block``/``downgrade``, never ``clamp``). A caller's own tighter cap is
    respected; the only fall-back to a hard block is when the input alone already exceeds the budget
    (no output room) or the provider can't take an injected ceiling.
    """
    if frame.cap_tokens is None:
        return None
    projected_input = tokens.count(call.messages, call.model)
    allowance = frame.cap_tokens - frame.spent_tokens - projected_input
    kwarg = _CLAMP_KWARG.get(call.provider)
    if kwarg is None or allowance <= 0:
        reason = (
            f"pre-flight clamp: cannot fit call within the remaining token budget "
            f"(~{frame.cap_tokens - frame.spent_tokens} left, ~{projected_input} input; "
            f"provider={call.provider!r}, model={call.model}) — use on_exceed='block' to reject, "
            f"or raise the cap"
        )
        _emit_budget_event(
            "blocked", call=call, frame=frame, reason=reason, projected_tokens=projected_input
        )
        raise BudgetExceeded(reason)
    existing = (call.metadata.get("request_kwargs") or {}).get(kwarg)
    if existing is not None and int(existing) <= allowance:
        return None  # the caller's own cap already fits within the budget — leave it untouched
    target = allowance if existing is None else min(int(existing), allowance)
    _clamps.append(
        {"model": call.model, "kwarg": kwarg, "limit": target, "tags": dict(_current_tags())}
    )
    _emit_budget_event(
        "clamped",
        call=call,
        frame=frame,
        reason=f"injected {kwarg}={target} to bound output within the remaining token budget",
        projected_tokens=frame.spent_tokens + projected_input + target,
    )
    return Reroute(**{kwarg: target})


def downgrades() -> list[dict]:
    """The pre-flight model downgrades performed so far (``{"from", "to", "tags"}`` rows)."""
    return list(_downgrades)


def clamps() -> list[dict]:
    """The pre-flight token clamps applied so far (``{"model", "kwarg", "limit", "tags"}`` rows)."""
    return list(_clamps)


def _on_call(call: object) -> None:
    """Bus subscriber: record spend by active tags and enforce active budgets."""
    if not isinstance(call, LLMCall):
        return  # tokenguard only accounts for model calls
    unpriced = call.cost is None  # no cost -> unknown/unpriced model, a USD blind spot
    usd = call.cost.amount if call.cost is not None else Decimal("0")
    inp = call.usage.input_tokens if call.usage is not None else 0
    out = call.usage.output_tokens if call.usage is not None else 0
    rsn = call.usage.reasoning_tokens if call.usage is not None else 0

    # GLR-5: prefer the frames/tags captured at initiation (correct even for a stream drained out
    # of scope, or a consumer call after a Python stream generator leaked+restored scopes); fall
    # back to the delivery-time contextvars only when nothing was attached (split-brain: the event
    # was built by a second cendor-core copy whose ambient provider we never ran).
    attached = call.metadata.get(_TG_ATTACH_KEY)
    frames = attached[0] if attached is not None else _budgets.get()
    if unpriced:
        # A USD-cap budget can't enforce against a $0-recorded call. Warn once per model, naming the
        # innermost USD-cap frame's mode. (block/downgrade already warned pre-flight, so this covers
        # the post-flight modes: raise/truncate/callable.)
        usd_frame = next((f for f in reversed(frames) if f.cap_usd is not None), None)
        if usd_frame is not None:
            mode = usd_frame.on_exceed
            _warn_unpriced(call.model, mode if isinstance(mode, str) else "callable")

    tags = dict(attached[1]) if attached is not None else dict(_current_tags())
    record = _Record(
        tags=tags,
        usd=usd,
        input_tokens=inp,
        output_tokens=out,
        reasoning_tokens=rsn,
        model=call.model,
        unpriced=unpriced,
    )
    # append + FIFO eviction is a read-modify-write on shared state; lock so concurrent emits
    # (threads sharing the process bus) can't corrupt the buffer or miscount _dropped.
    with _records_lock:
        global _dropped
        _records.append(record)
        if _max_records is not None and len(_records) > _max_records:
            overflow = len(_records) - _max_records
            del _records[:overflow]  # evict oldest (FIFO); counted, never silently
            _dropped += overflow
    if _sink is not None:
        _sink.write(
            {
                "tags": tags,
                "usd": str(usd),
                "input_tokens": inp,
                "output_tokens": out,
                "reasoning_tokens": rsn,
                "model": call.model,
            }
        )

    for frame in frames:
        frame.spent_usd += usd
        frame.spent_tokens += inp + out
        frame.calls += 1

    for frame in reversed(frames):  # enforce the tightest (innermost) breached cap first
        if _over(frame):
            if frame.on_exceed in ("downgrade", "clamp"):
                continue  # handled pre-flight; a no-op here must not mask an outer cap's action
            _enforce(frame, call)
            break


def _over(frame: _Frame) -> bool:
    if frame.cap_usd is not None and frame.spent_usd > frame.cap_usd:
        return True
    if frame.cap_tokens is not None and frame.spent_tokens > frame.cap_tokens:
        return True
    return False


def _enforce(frame: _Frame, call: LLMCall) -> None:
    on_exceed = frame.on_exceed
    if callable(on_exceed):
        on_exceed(
            {
                "frame": frame,
                "call": call,
                "spent_usd": frame.spent_usd,
                "cap_usd": frame.cap_usd,
            }
        )
        return
    if on_exceed == "truncate":
        raise _Truncated()
    if on_exceed in ("downgrade", "clamp"):
        return  # already handled pre-flight (interceptor rerouted the model / clamped the cap)
    raise BudgetExceeded(
        f"budget exceeded: spent ${frame.spent_usd} > cap ${frame.cap_usd} "
        f"after {frame.calls} call(s); last model={call.model}. "
        f"on_exceed='raise' is post-flight, so the cap is crossed by this one in-flight call — "
        f"use on_exceed='block' for a pre-flight hard cap that never overspends."
    )


class _Budget:
    """Both a decorator and a context manager. Created via :func:`budget`."""

    def __init__(
        self,
        usd: float | Decimal | None = None,
        tokens: int | None = None,
        on_exceed: OnExceedMode | Callable[[dict], Any] = "raise",
        scope: str | None = None,
        downgrade: dict | None = None,
        output_reserve: int = _DEFAULT_OUTPUT_RESERVE,
        reasoning_reserve: int = 0,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        if not callable(on_exceed) and on_exceed not in _ON_EXCEED:
            raise ValueError(
                f"on_exceed must be a callable or one of {_ON_EXCEED}, got {on_exceed!r}"
            )
        if usd is None and tokens is None:
            raise ValueError("budget requires a cap: pass usd= and/or tokens=")
        if on_exceed == "downgrade":
            if not downgrade:
                raise ValueError("on_exceed='downgrade' requires a downgrade={model: cheaper} map")
            if usd is None:
                raise ValueError(
                    "on_exceed='downgrade' requires a usd= cap (the projection is USD-based)"
                )
        if on_exceed == "clamp" and tokens is None:
            raise ValueError(
                "on_exceed='clamp' requires a tokens= cap (it injects a provider token ceiling)"
            )
        self._cap_usd = Decimal(str(usd)) if usd is not None else None
        self._cap_tokens = tokens
        self._on_exceed = on_exceed
        self._scope = scope
        self._downgrade = downgrade
        self._output_reserve = output_reserve
        self._reasoning_reserve = reasoning_reserve
        self._name = name
        self._description = description
        self.frame: _Frame | None = None
        self._token: Token | None = None

    def _open(self) -> None:
        _ensure_subscribed()
        self.frame = _Frame(
            self._cap_usd,
            self._cap_tokens,
            self._on_exceed,
            self._scope,
            self._downgrade,
            self._output_reserve,
            self._reasoning_reserve,
            name=self._name,
            description=self._description,
        )
        self._token = _budgets.set(_budgets.get() + (self.frame,))

    def _close(self) -> None:
        if self._token is not None:
            _budgets.reset(self._token)
            self._token = None

    def __enter__(self) -> _Budget:
        self._open()
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, tb: object) -> bool:
        self._close()
        return exc_type is _Truncated  # swallow the degradation signal; let real errors propagate

    @property
    def spent(self) -> Money:
        """Spend recorded against this budget so far (the context-manager frame).

        Meaningful inside a ``with budget(...) as b:`` block; as a decorator each call runs on its
        own cloned frame, so read spend via :func:`report` instead.
        """
        return Money(self.frame.spent_usd) if self.frame is not None else Money.zero()

    def _clone(self) -> _Budget:
        return _Budget(
            usd=self._cap_usd,
            tokens=self._cap_tokens,
            on_exceed=self._on_exceed,
            scope=self._scope,
            downgrade=self._downgrade,
            output_reserve=self._output_reserve,
            reasoning_reserve=self._reasoning_reserve,
            name=self._name,
            description=self._description,
        )

    def __call__(self, func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                guard = self._clone()
                guard._open()
                try:
                    return await func(*args, **kwargs)
                except _Truncated:
                    return None  # degraded gracefully instead of crashing
                finally:
                    guard._close()

            return awrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            guard = self._clone()
            guard._open()
            try:
                return func(*args, **kwargs)
            except _Truncated:
                return None
            finally:
                guard._close()

        return wrapper


def budget(
    usd: float | Decimal | None = None,
    tokens: int | None = None,
    on_exceed: OnExceedMode | Callable[[dict], Any] = "raise",
    scope: str | None = None,
    downgrade: dict | None = None,
    output_reserve: int = _DEFAULT_OUTPUT_RESERVE,
    reasoning_reserve: int = 0,
    name: str | None = None,
    description: str | None = None,
) -> _Budget:
    """Cap spend on a unit of work, as a decorator or a context manager.

    Not curried — call ``budget(...)`` directly (in TypeScript it's ``budget(cfg)(fn)``). The
    returned object is **both** a decorator and a context manager; there is no ``with_budget``.

    ```python
    from cendor.tokenguard import budget

    @budget(usd=0.50, on_exceed="raise")          # as a decorator
    def answer(q: str) -> str: ...

    with budget(usd=0.50) as b:                    # or as a context manager
        answer("hi")
        print(b.spent)                             # -> Money spent so far

    # name= gives the budget a human identity that rides every BudgetEvent it fires, so an
    # audit stream / monitor shows *which* budget blocked a call (not just "a budget did").
    with budget(usd=5, name="per-run cap", description="hard ceiling per support run"):
        answer("hi")
    ```

    Validates its configuration eagerly: a missing cap, an unknown ``on_exceed``, a ``"downgrade"``
    without a map/USD cap, or a ``"clamp"`` without a ``tokens=`` cap raises :class:`ValueError` at
    creation — never a silent no-op budget.

    **Hard cap vs. runaway guard:** ``"raise"`` is *post-flight* — it stops a loop before the next
    call, but the call that crosses the cap has already run, so spend overshoots by one call. For a
    cap that must **never** be exceeded, use ``on_exceed="block"`` (*pre-flight*: it projects cost
    and refuses the over-budget call before it runs).

    **Reasoning models:** a reasoning/thinking model's hidden thinking can't be predicted
    pre-flight, so no projection can bound one call in advance. Two mechanisms handle them: the
    cumulative gate (``"raise"``/``"block"``) enforces on the *exact* recorded usage — which already
    includes reasoning — so a runaway loop still stops; and ``on_exceed="clamp"`` injects the
    provider's own output ceiling (``max_completion_tokens`` / ``max_tokens``, which include
    reasoning) so a single call is capped *server-side* to the remaining token budget. See
    docs/tokenguard.md §7.

    Args:
        usd: Maximum USD spend before the breaker trips.
        tokens: Maximum total tokens before the breaker trips. At least one of ``usd``/``tokens``
            is required (``"clamp"`` requires ``tokens``).
        on_exceed: ``"raise"`` (post-flight: raise :class:`BudgetExceeded` once a returning call
            pushes spend over the cap — stops a runaway loop before the *next* call), ``"block"``
            (**pre-flight**: raise :class:`BudgetExceeded` *before* a call that would breach the cap
            runs, so it never executes — a true circuit breaker, projection-based), ``"clamp"``
            (**pre-flight**: inject a provider output ceiling so the call can't exceed the remaining
            ``tokens=`` budget — the only way to bound a reasoning model's runtime spend; requires a
            ``tokens=`` cap and supported on OpenAI/Anthropic, else falls back to a block — see
            :func:`clamps`), ``"truncate"`` (degrade gracefully — the decorated function returns
            ``None`` / the ``with`` block exits cleanly), ``"downgrade"`` (pre-flight reroute to a
            cheaper model, never raises), or a callable invoked with a context dict.
        scope: Optional label (e.g. ``"session"``) for nested budgets.
        downgrade: For ``on_exceed="downgrade"`` — a ``{model: cheaper_model}`` map (required for
            that mode). When a call would push the budget over its USD cap, it's rerouted to the
            mapped cheaper model *before* it runs. See :func:`downgrades`.
        output_reserve: Output tokens to assume in the pre-flight projection (``block``/
            ``downgrade``) when the request carries no ``max_tokens`` /
            ``max_completion_tokens``. Defaults to 256. (``clamp`` ignores it — it always caps
            output to the full remaining token budget.)
        reasoning_reserve: Extra output tokens to assume pre-flight for a reasoning model's hidden
            thinking, added to ``output_reserve`` *only* when the request sets no explicit output
            cap (an explicit cap already includes reasoning on OpenAI/Anthropic). Defaults to 0 —
            raise it to make the projection conservative for uncapped reasoning-model calls.
        name: Optional human identity for this budget, carried on every :class:`BudgetEvent` it
            fires (and mirrored to ``cendor.audit.budget`` by ``acttrace``), so a monitor/audit
            stream shows *which* budget acted. Keep it a **bounded identifier** (a fixed label like
            ``"per-run cap"``, not a per-request string) — it is also a governance-counter
            attribute, so an unbounded value explodes a metrics backend's cardinality.
        description: Optional longer human description of what the budget guards; carried on the
            :class:`BudgetEvent` and mirrored (truncated) to ``cendor.audit.description``.
    """
    return _Budget(
        usd=usd,
        tokens=tokens,
        on_exceed=on_exceed,
        scope=scope,
        downgrade=downgrade,
        output_reserve=output_reserve,
        reasoning_reserve=reasoning_reserve,
        name=name,
        description=description,
    )


@contextmanager
def track(**tags: object) -> Iterator[None]:
    """Attribute spend by ambient tags (feature / user_id / session_id), via ``contextvars``.

    Tags merge with any enclosing ``track(...)`` and apply to every instrumented call made
    inside the block — including across nested and async calls.

    ```python
    from cendor.tokenguard import track, report
    with track(feature="support", user_id="alice"):
        client.chat.completions.create(...)
    report(group_by=["feature"])   # spend grouped by tag
    ```
    """
    _ensure_subscribed()
    token = _tags.set({**_current_tags(), **tags})
    try:
        yield
    finally:
        _tags.reset(token)


def estimate(model: str, messages: list[dict], max_output_tokens: int = 0) -> Money:
    """Pre-flight cost projection without making a call (budget "linting"). docs/tokenguard.md §3.

    Prices the input via ``core.tokens`` × ``core.prices``, plus ``max_output_tokens`` of output.
    Output defaults to ``0`` (input-only) — pass an expected ``max_output_tokens`` to include it.
    The ``block``/``downgrade`` projections instead use the budget's ``output_reserve`` (def. 256).
    """
    input_tokens = tokens.count(messages, model)
    return prices.estimate(model, input_tokens, max_output_tokens)


@dataclass
class Report:
    """Aggregated spend rows. Iterable; cost-as-a-test-assertion via :meth:`assert_under`."""

    rows: list[dict] = field(default_factory=list)

    def __iter__(self) -> Iterator[dict]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def total(self) -> Money:
        return Money(sum((row["usd"].amount for row in self.rows), Decimal("0")))

    def assert_under(self, usd: float, **tag_filter: object) -> bool:
        """Assert spend (optionally filtered by tags) is under ``usd``, else ``AssertionError``."""
        cap = Decimal(str(usd))
        spent = sum(
            (
                row["usd"].amount
                for row in self.rows
                if all(row["tags"].get(k) == v for k, v in tag_filter.items())
            ),
            Decimal("0"),
        )
        if spent > cap:
            where = tag_filter or "all spend"
            raise AssertionError(f"${spent} exceeds cap ${cap} for {where}")
        return True


def report(group_by: list[str] | None = None) -> Report:
    """Aggregate recorded spend, grouped by the given tag keys. docs/tokenguard.md §3, §5.

    Returns rows of ``{"tags", "usd", "tokens", "input_tokens", "output_tokens",
    "reasoning_tokens", "calls", "unpriced_calls"}``. ``reasoning_tokens`` is the portion of
    ``output_tokens`` spent on reasoning (0 for non-reasoning calls, and for providers that report
    no separate count) — it's a breakdown, so it is *not* added into ``tokens``. ``unpriced_calls``
    is how many of the group's ``calls`` had no cost (unknown/unpriced model), so their USD is $0 —
    a blind spot for any USD cap (see also the module-level :func:`unpriced_calls`). Aggregates over
    the retained in-memory buffer; if the :func:`configure` cap has evicted older rows (see
    :func:`dropped`), the report reflects only the most recent window — use a sink for complete,
    durable history.

    Row keys stay **snake_case** in both languages (``row["input_tokens"]``, not ``inputTokens``):

    ```python
    from cendor.tokenguard import report
    for row in report(group_by=["feature"]):
        print(row["tags"], row["usd"], row["input_tokens"])
    ```
    """
    keys = group_by or []
    groups: dict[tuple, dict] = {}
    with _records_lock:
        records = list(_records)  # snapshot so a concurrent emit can't resize mid-iteration
    for rec in records:
        gk = tuple(rec.tags.get(k) for k in keys)
        group = groups.setdefault(
            gk,
            {
                "tags": {k: rec.tags.get(k) for k in keys},
                "usd": Decimal("0"),
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "calls": 0,
                "unpriced_calls": 0,
            },
        )
        group["usd"] += rec.usd
        group["input_tokens"] += rec.input_tokens
        group["output_tokens"] += rec.output_tokens
        group["reasoning_tokens"] += rec.reasoning_tokens
        group["calls"] += rec.calls
        if rec.unpriced:
            group["unpriced_calls"] += rec.calls

    rows = [
        {
            "tags": g["tags"],
            "usd": Money(g["usd"]),
            "tokens": g["input_tokens"] + g["output_tokens"],
            "input_tokens": g["input_tokens"],
            "output_tokens": g["output_tokens"],
            "reasoning_tokens": g["reasoning_tokens"],
            "calls": g["calls"],
            "unpriced_calls": g["unpriced_calls"],
        }
        for g in groups.values()
    ]
    return Report(rows)


def reset() -> None:
    """Clear recorded spend and active context (tags/budgets), and re-arm the bus subscription.

    Useful between tests so spend doesn't leak across cases.
    """
    global _sink, _dropped, _max_records, _on_unpriced
    with _records_lock:
        _records.clear()
        _dropped = 0
    _downgrades.clear()
    _clamps.clear()
    _warned_unpriced.clear()
    _sink = None
    _max_records = _DEFAULT_MAX_RECORDS
    _on_unpriced = _DEFAULT_ON_UNPRICED
    _tags.set({})
    _budgets.set(())
    _ensure_subscribed()


# Attach report() onto track for the documented `track.report(...)` ergonomic.
track.report = report  # type: ignore[attr-defined]

# Subscribe at import so even a bare instrumented call (no budget/track) is aggregated.
_ensure_subscribed()
