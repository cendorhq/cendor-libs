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

import inspect
from collections.abc import Sequence
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

from . import rules
from .decision import (
    ACTIONS,
    ALLOW,
    STAGES,
    Context,
    Guardrail,
    GuardrailDecision,
    GuardrailTripped,
    Verdict,
    guardrail,
    normalize_stages,
)

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
    # built-in rules
    "rules",
]

Guardrails = Sequence[Guardrail]


# --------------------------------------------------------------------------- engine


def _applicable(guardrails: Guardrails, stage: str) -> list[Guardrail]:
    return [g for g in guardrails if stage in g.stages]


def _emit(g: Guardrail, stage: str, verdict: Verdict, ctx: Context) -> GuardrailDecision:
    decision = GuardrailDecision(
        guardrail=g.name,
        stage=stage,
        action=verdict.action,
        reason=verdict.reason,
        agent=ctx.agent,
        tool=ctx.tool,
        trace_id=ctx.trace_id or current_trace_id(),
        metadata=dict(ctx.metadata),
    )
    bus.emit(decision)  # acttrace (if attached) chains this as a guardrail_decision entry
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


def evaluate(
    guardrails: Guardrails,
    stage: str,
    payload: Any,
    ctx: Context | None = None,
) -> tuple[Any, list[GuardrailDecision]]:
    """Run the ``stage`` guardrails over ``payload`` synchronously.

    Returns ``(payload, decisions)`` where ``payload`` carries any redactions applied in order.
    Raises :class:`GuardrailTripped` on the first ``block`` (fail-closed) — the decision is emitted
    first, so the block is on the audit chain before the exception propagates.

    Sync only: an ``async`` check raises ``TypeError`` here (use :func:`evaluate_async`).
    """
    ctx = ctx or Context(stage=stage)
    decisions: list[GuardrailDecision] = []
    for g in _applicable(guardrails, stage):
        if inspect.iscoroutinefunction(g.check):
            raise TypeError(
                f"guardrail {g.name!r} has an async check; use evaluate_async / an async run"
            )
        verdict = g.check(payload, ctx)
        if inspect.isawaitable(verdict):
            close = getattr(verdict, "close", None)
            if close is not None:
                close()  # avoid an un-awaited-coroutine warning
            raise TypeError(
                f"guardrail {g.name!r} returned an awaitable; use evaluate_async / an async run"
            )
        payload = _handle(verdict, g, stage, payload, ctx, decisions)
    return payload, decisions


async def evaluate_async(
    guardrails: Guardrails,
    stage: str,
    payload: Any,
    ctx: Context | None = None,
) -> tuple[Any, list[GuardrailDecision]]:
    """Async counterpart of :func:`evaluate`: awaits ``async`` checks, calls sync ones directly."""
    ctx = ctx or Context(stage=stage)
    decisions: list[GuardrailDecision] = []
    for g in _applicable(guardrails, stage):
        verdict = g.check(payload, ctx)
        if inspect.isawaitable(verdict):
            verdict = await verdict
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
    redacted payload back, use :func:`evaluate` or :func:`install`."""
    return evaluate(guardrails, stage, payload, ctx)[1]


async def apply_async(
    guardrails: Guardrails,
    stage: str,
    payload: Any,
    ctx: Context | None = None,
) -> list[GuardrailDecision]:
    """Async counterpart of :func:`apply`."""
    return (await evaluate_async(guardrails, stage, payload, ctx))[1]


# --------------------------------------------------------------------------- standalone wiring

#: Module-global install state. Only one set of guardrails is installed on the seam at a time;
#: :func:`install` replaces a prior install cleanly (its interceptor + subscriber are torn down).
_state: dict[str, Any] = {"interceptor": None, "subscriber": None}


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
    :func:`uninstall` to remove.
    """
    uninstall()
    gl = list(guardrails)

    def _interceptor(event: Any) -> Any:
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

    def _subscriber(event: Any) -> None:
        if not isinstance(event, LLMCall):
            return
        text = _response_text(event)
        if text is None:
            return
        ctx = Context(stage="output", trace_id=event.trace_id)
        evaluate(gl, "output", text, ctx)  # block raises post-flight

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


def _response_text(call: LLMCall) -> str | None:
    """Best-effort assistant text off a completed ``LLMCall`` for the standalone output stage.

    Reads the raw provider response at ``call.metadata["response"]`` across common shapes (OpenAI
    Chat Completions / Responses, Anthropic, Ollama, Gemini, Bedrock). Returns ``None`` when nothing
    is extractable, so output guardrails simply skip rather than misfire. The SDK's in-loop output
    stage has the parsed text directly and does not rely on this.
    """
    response = call.metadata.get("response")
    if response is None:
        return None
    try:
        return _extract_text(response)
    except Exception:  # noqa: BLE001 - extraction must never break the passthrough
        return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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
