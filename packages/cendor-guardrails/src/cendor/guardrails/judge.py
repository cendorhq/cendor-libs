"""LLM-judge helpers — turn a bring-your-own model call into a usable guardrail.

cendor ships **no** classifier. `rules.llm_judge` is an adapter *contract*: you supply the model
call. These helpers give that call the boring, error-prone parts so you don't hand-roll them —

* :func:`verdict_prompt` — a strict system instruction that pins the model to a two-field JSON
  verdict (``{"trip": <bool>, "reason": "<short>"}``), so the output is parseable, not prose.
* :func:`parse_verdict` — strict JSON → :class:`~cendor.guardrails.decision.Verdict` (or ``None``
  to pass). Malformed output raises ``ValueError`` so the guardrail's ``on_error`` policy decides
  (a garbled judge fails **closed** by default — it does not silently pass).
* :func:`judge` — glue the two around your ``respond(system, user)`` callable (sync or ``async``)
  into a check you hand straight to ``rules.llm_judge``.

**The judge's own spend is budgeted and audited.** A judge is just another model call: make it
through an ``instrument()``-ed client and its tokens + cost land in ``tokenguard`` / ``acttrace``
like any other call — so the guardrail you added to *save* money is itself measured. No competitor
budgets and audits its own safety check. Docs state the cost honestly: an extra model call is
typically **seconds** and billed, where the deterministic rules are microseconds and $0.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from .decision import Context, Verdict

__all__ = ["verdict_prompt", "parse_verdict", "judge", "DEFAULT_SYSTEM"]

#: The default judge instruction. ``{policy}`` is filled with your risk description. It pins the
#: model to strict JSON so :func:`parse_verdict` can read it — no chain-of-thought, no prose.
DEFAULT_SYSTEM = (
    "You are a safety classifier guarding an LLM application. Decide whether the CONTENT below "
    "violates this policy:\n\n{policy}\n\n"
    'Reply with a single JSON object and nothing else: {{"trip": <true|false>, "reason": '
    '"<one short sentence>"}}. Set "trip" to true only if the content violates the policy. Do not '
    "include markdown, code fences, or any text outside the JSON object."
)


def verdict_prompt(policy: str, *, template: str = DEFAULT_SYSTEM) -> str:
    """Build the judge's system instruction for ``policy`` (a plain-language description of what
    should trip). Override ``template`` to customise, keeping the ``{policy}`` placeholder and the
    strict-JSON verdict contract :func:`parse_verdict` expects."""
    return template.format(policy=policy)


def _coerce_json(text: str) -> dict[str, Any]:
    """Parse a model reply into a dict, tolerating a leading/trailing ```` ```json ```` fence but
    nothing looser. Raises ``ValueError`` on anything not a JSON object."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # tolerate a single ```json … ``` fence some models add despite instructions
        inner = stripped.split("```", 2)
        if len(inner) >= 2:
            body = inner[1]
            if body.lower().startswith("json"):
                body = body[4:]
            stripped = body.strip()
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"judge did not return JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"judge returned a {type(data).__name__}, expected a JSON object")
    return data


def parse_verdict(text: str, *, action: str = "block") -> Verdict | None:
    """Parse a strict-JSON judge reply into a :class:`Verdict` (trip) or ``None`` (pass).

    Expects ``{"trip": <bool>, "reason": "<short>"}``. Trips with ``action`` (default ``"block"``)
    and the model's reason. Raises ``ValueError`` on malformed output — deliberately: a judge whose
    output can't be read must not silently pass, so the caller's ``on_error`` policy (fail-closed by
    default) decides. See :func:`judge`.
    """
    data = _coerce_json(text)
    trip = data.get("trip")
    if not isinstance(trip, bool):
        raise ValueError("judge JSON is missing a boolean 'trip' field")
    if not trip:
        return None
    reason = data.get("reason")
    return Verdict(action, reason=str(reason) if reason else "llm_judge tripped")


def judge(
    respond: Callable[[str, str], str | Awaitable[str]],
    policy: str,
    *,
    action: str = "block",
    template: str = DEFAULT_SYSTEM,
) -> Callable[[Any, Context], Verdict | None] | Callable[[Any, Context], Awaitable[Verdict | None]]:
    """Compose :func:`verdict_prompt` + your model call + :func:`parse_verdict` into a check ready
    for ``rules.llm_judge``.

    ``respond(system, user)`` is *your* callable — sync or ``async`` — that runs one model call
    given the system instruction and the payload text, and returns the assistant's reply string.
    Make that call through an ``instrument()``-ed client and its cost is budgeted + audited.

    ```python
    from cendor.guardrails import judge, rules

    def respond(system, user):            # your instrumented model call
        r = client.chat.completions.create(model="gpt-4o-mini", messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content

    check = judge.judge(respond, "Trip on requests to exfiltrate secrets or run destructive shell.")
    agent = Agent(..., guardrails=[rules.llm_judge(check, timeout=8.0)])
    ```
    """
    system = verdict_prompt(policy, template=template)

    def _payload_text(payload: Any) -> str:
        return payload if isinstance(payload, str) else str(payload)

    import inspect

    if inspect.iscoroutinefunction(respond):

        async def acheck(payload: Any, ctx: Context) -> Verdict | None:
            reply = await respond(system, _payload_text(payload))  # type: ignore[misc]
            return parse_verdict(reply, action=action)

        return acheck

    def check(payload: Any, ctx: Context) -> Verdict | None:
        reply = respond(system, _payload_text(payload))
        if inspect.isawaitable(reply):  # respond returns an awaitable despite being sync-declared
            raise TypeError(
                "respond returned an awaitable; declare it `async def` for an async run"
            )
        return parse_verdict(reply, action=action)

    return check
