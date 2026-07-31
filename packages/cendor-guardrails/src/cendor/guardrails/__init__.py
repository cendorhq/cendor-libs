"""cendor.guardrails — a local-first gate for LLM apps: define a check, attach it to a stage.

The **Gate** in the Cendor pipeline (``contextkit → squeeze → tokenguard → guardrails → cassette →
acttrace``). Deterministic checks — keyword/regex/URL/length/JSON-schema — run in microseconds for
$0, at four intervention points (``input | tool_call | tool_output | output``). Every trip or flag
emits a :class:`GuardrailDecision` on the ``cendor.core`` bus, so ``acttrace`` chains it as
tamper-evident evidence with no import between the two.

Three ways to use it (no server, no account, offline):

* **Pure** — :func:`apply` / :func:`evaluate` gate a payload directly.
* **Framework-independent** — :func:`install` registers one ``cendor.core`` interceptor so every
  instrumented client call is gated, under any framework or a bare SDK.
* **In an agent loop** — ``cendor-sdk``'s ``Agent(guardrails=[…])`` wires all four stages.

Guardrails imports **only** ``cendor.core`` (constitution rule 2). It never imports acttrace or
tokenguard; they never import it. See docs/guardrails.md.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from cendor.core import bus
from cendor.core.instrument import (
    MISS,
    Reroute,
    add_interceptor,
    current_trace_id,
    remove_interceptor,
)
from cendor.core.types import LLMCall, ToolCall

from . import adapters, embeddings, intent, judge, policy, presets, redteam, rules, semantic
from .decision import (
    ACTIONS,
    ALLOW,
    ON_ERROR,
    STAGES,
    Context,
    Guardrail,
    GuardrailDecision,
    GuardrailTripped,
    Verdict,
    guardrail,
    normalize_stages,
)
from .policy import LoadedPolicy, load_policy, policy_schema
from .redteam import AttackCase, RedTeamReport, load_corpus, run_redteam, run_redteam_async

__all__ = [
    # types
    "Guardrail",
    "guardrail",
    "Verdict",
    "Context",
    "GuardrailDecision",
    "GuardrailTripped",
    "STAGES",
    "ACTIONS",
    "ON_ERROR",
    "ALLOW",
    "normalize_stages",
    # engine
    "apply",
    "apply_async",
    "evaluate",
    "evaluate_async",
    # standalone wiring
    "install",
    "uninstall",
    "scoped",
    "use_meter",
    # config-as-data
    "load_policy",
    "LoadedPolicy",
    "policy_schema",
    # red-team evaluation (measure trip rate against a labeled corpus you supply)
    "load_corpus",
    "run_redteam",
    "run_redteam_async",
    "AttackCase",
    "RedTeamReport",
    # built-in rules + judge helpers + opt-in detection-tier adapters + similarity checks
    "rules",
    "judge",
    "adapters",
    "semantic",
    "intent",
    "embeddings",
    "presets",
    "policy",
    "redteam",
]

Guardrails = Sequence[Guardrail]


# --------------------------------------------------------------------------- engine

# --- G15: native governance counter (optional, no-op without OpenTelemetry) ---
#: Lazily-created ``cendor.guardrails.decisions`` counter (meter ``cendor.guardrails``). ``None``
#: until first use; stays ``None`` if OTel isn't installed. Renders as
#: ``cendor_guardrails_decisions_total`` in Prometheus.
_decisions_counter: Any = None
_decisions_counter_checked = False


def use_meter(meter: Any) -> None:
    """Create the ``cendor.guardrails.decisions`` counter on an explicit OpenTelemetry ``Meter``.

    By default the counter comes from the **global** meter provider
    (``metrics.get_meter("cendor.guardrails")``), lazily on the first decision. Call this to point
    it at a meter you own instead: a test's in-memory reader, an isolated provider in a
    multi-tenant host, or a second pipeline. The counter name and its bounded ``guardrail`` /
    ``stage`` / ``action`` labels are identical either way, and this never changes whether a
    decision is taken — the counter is observability, not a gate.

    ``use_meter(None)`` restores the default (the global provider is re-read on the next decision).

    ```python
    from cendor import guardrails
    guardrails.use_meter(my_provider.get_meter("cendor.guardrails"))  # or None to reset
    ```

    The seam exists because there was no way to read this counter without installing a
    process-global meter provider; the external black-box suite filed it as a product improvement,
    having had to install one to assert anything.
    """
    global _decisions_counter, _decisions_counter_checked
    if meter is None:
        _decisions_counter = None
        _decisions_counter_checked = False  # re-read the global provider on the next decision
        return
    _decisions_counter = meter.create_counter("cendor.guardrails.decisions")
    _decisions_counter_checked = True


def _decisions_add(attrs: dict[str, Any]) -> None:
    """Increment the ``cendor.guardrails.decisions`` counter (no-op without OpenTelemetry).

    A lazily-created counter on a proxy meter binds to whatever ``MeterProvider`` the host app
    configures (before or after first use). Best-effort observability — it never gates the decision.
    Labels are the bounded sets ``guardrail`` / ``stage`` / ``action``. :func:`use_meter` overrides
    which meter it is created on.
    """
    global _decisions_counter, _decisions_counter_checked
    if not _decisions_counter_checked:
        _decisions_counter_checked = True
        try:
            from opentelemetry import metrics
        except ImportError:
            _decisions_counter = None
        else:
            _decisions_counter = metrics.get_meter("cendor.guardrails").create_counter(
                "cendor.guardrails.decisions"
            )
    if _decisions_counter is not None:
        # "Best-effort" has to be true in the code, not just the docstring. Measured 2026-07-31
        # (GAPCLOSE S8): a counter whose `add` raised propagated out of `apply()` and took the
        # GOVERNANCE DECISION with it — a metrics backend could fail a guardrail. A real OTel
        # counter does not raise, so this only ever bit a custom or injected meter; it is guarded
        # because the failure mode is exactly backwards for this library.
        try:
            _decisions_counter.add(1, attrs)
        except Exception:  # noqa: BLE001 - observability must never gate a decision
            pass


def _applicable(guardrails: Guardrails, stage: str) -> list[Guardrail]:
    return [g for g in guardrails if stage in g.stages]


def _emit(g: Guardrail, stage: str, verdict: Verdict, ctx: Context) -> GuardrailDecision:
    # Three metadata layers, lowest precedence first: the guardrail's static metadata (e.g.
    # load_policy's policy_hash/version) is the base; the verdict's per-result annotations (the
    # reserved severity/detected/… keys an adapter attaches to *this* check — see bus-events.md)
    # layer over it; the caller's per-call Context.metadata is on top and still wins any key clash.
    metadata = {**g.metadata, **verdict.metadata, **ctx.metadata}
    decision = GuardrailDecision(
        guardrail=g.name,
        stage=stage,
        action=verdict.action,
        reason=verdict.reason,
        agent=ctx.agent,
        tool=ctx.tool,
        trace_id=ctx.trace_id or current_trace_id(),
        metadata=metadata,
    )
    bus.emit(decision)  # acttrace (if attached) chains this as a guardrail_decision entry
    # G15: native governance counter (no-op without OpenTelemetry). Bounded label set.
    _decisions_add({"guardrail": g.name, "stage": stage, "action": verdict.action})
    return decision


def _handle(
    verdict: Verdict | None,
    g: Guardrail,
    stage: str,
    payload: Any,
    ctx: Context,
    decisions: list[GuardrailDecision],
) -> Any:
    """Emit + record one verdict; apply a redaction to ``payload``; raise on a block. Returns the
    (possibly redacted) payload to carry into the next check."""
    if verdict is None:
        return payload
    decisions.append(_emit(g, stage, verdict, ctx))
    if verdict.action == "block":
        raise GuardrailTripped(decisions)
    if verdict.action == "redact" and verdict.replacement is not None:
        return verdict.replacement
    return payload


_ERROR_REASON_MAX = 200


def _on_error_verdict(g: Guardrail, exc: BaseException) -> Verdict:
    """Map a check error/timeout to a verdict per ``g.on_error``. Either way this becomes a
    :class:`GuardrailDecision`, so the audit chain records that the check could not run — never a
    silently swallowed exception. The reason carries the exception *type* and a truncated message,
    never the payload the check saw."""
    detail = f"{type(exc).__name__}: {exc}"
    if len(detail) > _ERROR_REASON_MAX:
        detail = detail[:_ERROR_REASON_MAX] + "…"
    if g.on_error == "fail_open":
        return Verdict("flag", reason=f"check errored (fail-open): {detail}")
    return Verdict("block", reason=f"check errored (fail-closed): {detail}")


def _call_sync_with_timeout(g: Guardrail, payload: Any, ctx: Context) -> Any:
    """Call ``g.check`` synchronously, bounding it to ``g.timeout`` seconds via a daemon worker
    thread when set. A timeout raises ``TimeoutError`` (the worker is abandoned — daemon, so it
    dies with the process; keep timed sync checks side-effect-light)."""
    if g.timeout is None:
        return g.check(payload, ctx)
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = g.check(payload, ctx)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            box["error"] = exc

    worker = threading.Thread(target=_run, name=f"guardrail:{g.name}", daemon=True)
    worker.start()
    worker.join(g.timeout)
    if worker.is_alive():
        raise TimeoutError(f"guardrail {g.name!r} check exceeded {g.timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _invoke_sync(g: Guardrail, payload: Any, ctx: Context) -> Verdict | None:
    """Run one guardrail's check on the sync path, applying its timeout + on_error policy. An
    ``async`` check is a programming error here (use the async path), so that ``TypeError`` is
    raised *before* the on_error guard and always propagates."""
    if inspect.iscoroutinefunction(g.check):
        raise TypeError(
            f"guardrail {g.name!r} has an async check; use evaluate_async / an async run"
        )
    try:
        result = _call_sync_with_timeout(g, payload, ctx)
    except GuardrailTripped:
        raise
    except Exception as exc:  # noqa: BLE001 - timeout / check bug → on_error policy (recorded)
        return _on_error_verdict(g, exc)
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if close is not None:
            close()  # avoid an un-awaited-coroutine warning
        raise TypeError(
            f"guardrail {g.name!r} returned an awaitable; use evaluate_async / an async run"
        )
    return result


async def _invoke_async(g: Guardrail, payload: Any, ctx: Context) -> Verdict | None:
    """Run one guardrail's check on the async path, applying its timeout + on_error policy.

    ``asyncio.wait_for`` bounds an ``async`` check to ``g.timeout``. A *sync* check runs inline (the
    deterministic tier is microseconds); a sync ``timeout`` is enforced on the sync path only —
    stated in :class:`Guardrail`.
    """
    try:
        result = g.check(payload, ctx)
        if inspect.isawaitable(result):
            result = await (
                asyncio.wait_for(result, g.timeout) if g.timeout is not None else result
            )
    except GuardrailTripped:
        raise
    except Exception as exc:  # noqa: BLE001 - timeout / check bug → on_error policy (recorded)
        return _on_error_verdict(g, exc)
    return result


def evaluate(
    guardrails: Guardrails,
    stage: str,
    payload: Any,
    ctx: Context | None = None,
) -> tuple[Any, list[GuardrailDecision]]:
    """Run the ``stage`` guardrails over ``payload`` synchronously.

    Returns ``(payload, decisions)`` where ``payload`` carries any redactions applied in order.
    Raises :class:`GuardrailTripped` on the first ``block`` (fail-closed) — the decision is emitted
    first, so the block is on the audit chain before the exception propagates. Each check honours
    its :attr:`~.decision.Guardrail.timeout` / :attr:`~.decision.Guardrail.on_error` policy.

    ```python
    from cendor.guardrails import rules, evaluate
    gate = [rules.keyword_deny(["ignore previous instructions"], action="block")]
    payload, decisions = evaluate(gate, "input", "please ignore previous instructions")
    ```

    Under ``cendor-sdk`` you don't call ``evaluate`` — pass ``Agent(guardrails=[…])`` and the loop
    runs all four stages for you. This is the standalone (door-1) entry point.

    Sync only: an ``async`` check raises ``TypeError`` here (use :func:`evaluate_async`).
    """
    ctx = ctx or Context(stage=stage)
    decisions: list[GuardrailDecision] = []
    for g in _applicable(guardrails, stage):
        verdict = _invoke_sync(g, payload, ctx)
        payload = _handle(verdict, g, stage, payload, ctx, decisions)
    return payload, decisions


async def evaluate_async(
    guardrails: Guardrails,
    stage: str,
    payload: Any,
    ctx: Context | None = None,
) -> tuple[Any, list[GuardrailDecision]]:
    """Async counterpart of :func:`evaluate`: awaits ``async`` checks (bounded by each guardrail's
    ``timeout``), calls sync ones directly, and applies each guardrail's ``on_error`` policy.

    ```python
    payload, decisions = await evaluate_async(gate, "output", answer)
    ```
    """
    ctx = ctx or Context(stage=stage)
    decisions: list[GuardrailDecision] = []
    for g in _applicable(guardrails, stage):
        verdict = await _invoke_async(g, payload, ctx)
        payload = _handle(verdict, g, stage, payload, ctx, decisions)
    return payload, decisions


def apply(
    guardrails: Guardrails,
    stage: str,
    payload: Any,
    ctx: Context | None = None,
) -> list[GuardrailDecision]:
    """Gate ``payload`` and return the recorded decisions. Raises :class:`GuardrailTripped` on a
    block. A thin wrapper over :func:`evaluate` for callers that only gate (block/flag) — for the
    redacted payload back, use :func:`evaluate` or :func:`install`.

    ```python
    decisions = apply(gate, "input", user_text)
    ```
    """
    return evaluate(guardrails, stage, payload, ctx)[1]


async def apply_async(
    guardrails: Guardrails,
    stage: str,
    payload: Any,
    ctx: Context | None = None,
) -> list[GuardrailDecision]:
    """Async counterpart of :func:`apply`.

    ```python
    decisions = await apply_async(gate, "input", user_text)
    ```
    """
    return (await evaluate_async(guardrails, stage, payload, ctx))[1]


# --------------------------------------------------------------------------- standalone wiring

#: Module-global install state. Only one set of guardrails is installed on the seam at a time;
#: :func:`install` replaces a prior install cleanly (its interceptor + subscriber are torn down).
_state: dict[str, Any] = {"interceptor": None, "subscriber": None}


def _gate_interceptor(gl: Guardrails, event: Any) -> Any:
    """The shared interceptor body used by :func:`install` and :func:`scoped` — gate an ``LLMCall``
    at the ``input`` stage (redact reroutes, block raises, pass declines) and a ``ToolCall`` at the
    ``tool_call`` stage (block raises; redact/flag recorded but the call proceeds)."""
    if isinstance(event, LLMCall):
        ctx = Context(stage="input", trace_id=event.trace_id)
        cleaned, decisions = evaluate(gl, "input", event.messages, ctx)
        if any(d.action == "redact" for d in decisions):
            return Reroute(messages=cleaned)
        return MISS
    if isinstance(event, ToolCall):
        ctx = Context(
            stage="tool_call",
            tool=event.name,
            tool_args=event.arguments,
            trace_id=event.trace_id,
        )
        evaluate(gl, "tool_call", event.arguments, ctx)  # block raises; else record + proceed
        return MISS
    return MISS


def _gate_output(gl: Guardrails, event: Any) -> None:
    """The shared post-flight output subscriber body: gate the completed ``LLMCall``'s response
    text at the ``output`` stage (block raises after the call ran)."""
    if not isinstance(event, LLMCall):
        return
    text = _response_text(event)
    if text is None:
        return
    ctx = Context(stage="output", trace_id=event.trace_id)
    evaluate(gl, "output", text, ctx)  # block raises post-flight


def install(guardrails: Guardrails) -> None:
    """Gate every instrumented call by registering ONE ``cendor.core`` interceptor (+ an output
    subscriber). Framework-independent: works under any framework or a bare instrumented client.

    * **input** — an ``LLMCall`` is gated over ``call.messages`` before it runs: a block raises
      (nothing spends), a redact reroutes the cleaned messages to the provider, a pass declines.
    * **tool_call** — a ``ToolCall`` is gated over ``call.arguments``: a block raises; a redact/flag
      is recorded but the call proceeds (tools have no message-rewrite seam — block is the pre-send
      control there, mirroring ``acttrace``'s ``guard()``).
    * **output** — a bus subscriber inspects the *completed* ``LLMCall`` and raises **post-flight**
      on a block (the call already happened and was billed — same overshoot semantics as
      ``tokenguard``'s ``on_exceed="raise"``; the SDK's in-loop output stage pre-empts instead).

    Runs sync checks only (the interceptor seam is synchronous); an async check raises. Call
    :func:`uninstall` to remove. ``install()`` is **process-global** — for a concurrent server that
    varies guardrails per request, use :func:`scoped` instead.
    """
    uninstall()
    gl = list(guardrails)

    def _interceptor(event: Any) -> Any:
        return _gate_interceptor(gl, event)

    def _subscriber(event: Any) -> None:
        _gate_output(gl, event)

    add_interceptor(_interceptor)
    bus.subscribe(_subscriber)
    _state.update(interceptor=_interceptor, subscriber=_subscriber)


def uninstall() -> None:
    """Remove the interceptor + output subscriber registered by :func:`install` (idempotent)."""
    if _state["interceptor"] is not None:
        remove_interceptor(_state["interceptor"])
    if _state["subscriber"] is not None:
        bus.unsubscribe(_state["subscriber"])
    _state.update(interceptor=None, subscriber=None)


# --------------------------------------------------------------------------- scoped (per-request)

#: The guardrails active in *this* execution context (``contextvars``), or ``None`` outside any
#: :func:`scoped` block. A concurrent server (threads or asyncio tasks) sees its own value, so two
#: overlapping requests can run different guardrails through one shared, context-gated interceptor.
_scoped_guardrails: ContextVar[list[Guardrail] | None] = ContextVar(
    "cendor_guardrails_scoped", default=None
)


def _scoped_interceptor(event: Any) -> Any:
    gl = _scoped_guardrails.get()
    if not gl:
        return MISS  # no active scope in this context — decline
    return _gate_interceptor(gl, event)


def _scoped_subscriber(event: Any) -> None:
    gl = _scoped_guardrails.get()
    if not gl:
        return
    _gate_output(gl, event)


def _ensure_scoped_seam() -> None:
    """Register the context-gated interceptor + subscriber (idempotent — ``add_interceptor`` /
    ``bus.subscribe`` de-dupe). They stay registered and are no-ops outside a :func:`scoped` block,
    so leaving them installed costs a dict lookup per call and needs no teardown."""
    add_interceptor(_scoped_interceptor)
    bus.subscribe(_scoped_subscriber)


@contextmanager
def scoped(guardrails: Guardrails) -> Iterator[None]:
    """Gate every instrumented call for the duration of the block — like :func:`install`, but
    **scoped to the current execution context** rather than process-global.

    The interceptor reads the active guardrails from a ``ContextVar``, so a concurrent server
    (asyncio tasks or threads) can vary guardrails per request without one request's set leaking
    into another. This closes the same "process-global" wart for standalone (door-1) users that
    ``Agent(guardrails=[…])`` closed for the SDK. Nest freely — an inner ``scoped`` replaces the set
    for its block and the outer set is restored on exit. Runs sync checks only (the seam is sync).

    ```python
    with scoped([rules.keyword_deny(["secret"], action="block")]):
        client.chat.completions.create(...)   # gated here
    client.chat.completions.create(...)        # not gated
    ```
    """
    _ensure_scoped_seam()
    token = _scoped_guardrails.set(list(guardrails))
    try:
        yield
    finally:
        _scoped_guardrails.reset(token)


def _response_text(call: LLMCall) -> str | None:
    """Best-effort assistant text off a completed ``LLMCall`` for the standalone output stage.

    Reads the raw provider response at ``call.metadata["response"]`` across common shapes (OpenAI
    Chat Completions / Responses, Anthropic, Ollama, Gemini, Bedrock). For a **streamed** call
    ``cendor-core`` stores the raw delta chunks as a *list* there, so the completed text is
    reconstructed by joining the per-chunk deltas — otherwise the output stage silently no-ops on
    streamed responses (the banned text is delivered). Returns ``None`` when nothing is extractable,
    so output guardrails simply skip rather than misfire. The SDK's in-loop output stage has the
    parsed text directly and does not rely on this.

    Same reasoning for a **raw-response envelope** (``client.responses.with_raw_response.create(…)``
    — Microsoft Agent Framework's shape, and ``langchain-openai``'s): the envelope carries headers
    and an un-parsed body, so it has no assistant text and the output stage skipped it entirely.
    ``cendor-core`` ≥ 1.14.2 publishes the decoded payload at ``metadata["response_body"]``; prefer
    it, so those integrations are gated like any other call.
    """
    response = call.metadata.get("response_body") or call.metadata.get("response")
    if response is None:
        return None
    try:
        if isinstance(response, list):  # a streamed call: core stored the raw delta chunks
            text = "".join(_chunk_text(ch) for ch in response)
            return text or None
        return _extract_text(response)
    except Exception:  # noqa: BLE001 - extraction must never break the passthrough
        return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _chunk_text(chunk: Any) -> str:
    """Text carried by one streamed delta chunk, across providers. A chunk matches exactly one
    provider shape, so trying each and joining reconstructs the full assistant text. Mirrors
    ``cendor.core``'s internal per-provider stream-delta join (kept local — guardrails imports only
    core's public surface)."""
    # OpenAI / HuggingFace Chat Completions: choices[].delta.content
    choices = _get(chunk, "choices")
    if isinstance(choices, list) and choices:
        parts = [
            _get(_get(c, "delta"), "content")
            for c in choices
            if isinstance(_get(_get(c, "delta"), "content"), str)
        ]
        if parts:
            return "".join(parts)
    # OpenAI Responses API: response.output_text.delta events carry incremental text
    if _get(chunk, "type") == "response.output_text.delta":
        delta = _get(chunk, "delta")
        return delta if isinstance(delta, str) else ""
    # Anthropic: content_block_delta events with delta.text
    if _get(chunk, "type") == "content_block_delta":
        text = _get(_get(chunk, "delta"), "text")
        return text if isinstance(text, str) else ""
    # Ollama: message.content
    message = _get(chunk, "message")
    if message is not None:
        text = _get(message, "content")
        if isinstance(text, str):
            return text
    # Gemini: chunk.text
    text = _get(chunk, "text")
    if isinstance(text, str):
        return text
    # Bedrock Converse: contentBlockDelta.delta.text
    text = _get(_get(_get(chunk, "contentBlockDelta"), "delta"), "text")
    return text if isinstance(text, str) else ""


def _extract_text(response: Any) -> str | None:
    # OpenAI Responses API convenience field
    output_text = _get(response, "output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    # OpenAI / HuggingFace Chat Completions: choices[0].message.content
    choices = _get(response, "choices")
    if isinstance(choices, list) and choices:
        message = _get(choices[0], "message")
        content = _get(message, "content")
        if isinstance(content, str):
            return content
    # Anthropic: content is a list of blocks with .text
    content = _get(response, "content")
    if isinstance(content, list):
        parts = [str(_get(b, "text", "")) for b in content if _get(b, "text") is not None]
        if parts:
            return "".join(parts)
    # Ollama: message.content
    message = _get(response, "message")
    if message is not None:
        text = _get(message, "content")
        if isinstance(text, str):
            return text
    # Gemini: response.text
    text = _get(response, "text")
    if isinstance(text, str) and text:
        return text
    # Bedrock Converse: output.message.content[].text
    out_message = _get(_get(response, "output"), "message")
    out_content = _get(out_message, "content")
    if isinstance(out_content, list):
        parts = [str(_get(b, "text", "")) for b in out_content if _get(b, "text") is not None]
        if parts:
            return "".join(parts)
    return None
