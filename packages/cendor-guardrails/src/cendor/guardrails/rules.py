"""Deterministic, local-first built-in rules — the microsecond, $0 tier. docs/guardrails.md.

Every rule here is regex/arithmetic only: no model call, no network, no heavy dependency. Each
factory returns a :class:`~cendor.guardrails.decision.Guardrail` you attach to an agent, pass to
:func:`~cendor.guardrails.apply`, or ``install()`` on the core seam.

Deliberately **not** here: PII/secret detection (that is ``acttrace``'s detector catalogue — reach
for ``guard(Policy…)`` and keep one detection engine), ML classifiers, and jailbreak detection.
An LLM-judge is an *adapter contract* (:func:`llm_judge`), never a built-in — you supply the model
call, and the docs state its latency/cost honestly. Deterministic checks do not stop a novel
adversarial attack; see docs/guardrails.md "Honest limits".
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import urlsplit

from cendor.core import tokens

from .decision import Context, Guardrail, Verdict, normalize_stages

# --------------------------------------------------------------------------- payload helpers


def _payload_text(payload: Any) -> str:
    """Flatten a payload (a string, a chat-message list, or a single message dict) to scannable
    text. Multimodal content blocks contribute their text parts; non-text is ignored."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        # a chat message ({"role","content"}) → its text; any other dict (e.g. a ToolCall's
        # {"args": [...], "kwargs": {...}}) → its string form, so tool arguments are scannable.
        if "content" in payload or "role" in payload:
            return _message_text(payload)
        return str(payload)
    if isinstance(payload, list):
        return "\n".join(
            _message_text(m) if isinstance(m, dict) else str(m) for m in payload if m is not None
        )
    return str(payload)


def _message_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _redact_payload(payload: Any, sub: Callable[[str], str]) -> Any:
    """Apply ``sub`` to every text field of ``payload``, returning a new structure of the same
    shape (never mutating the caller's object)."""
    if isinstance(payload, str):
        return sub(payload)
    if isinstance(payload, dict):
        return _redact_message(payload, sub)
    if isinstance(payload, list):
        return [
            _redact_message(m, sub)
            if isinstance(m, dict)
            else (sub(m) if isinstance(m, str) else m)
            for m in payload
        ]
    return payload


def _redact_message(msg: dict, sub: Callable[[str], str]) -> dict:
    out = dict(msg)
    content = out.get("content")
    if isinstance(content, str):
        out["content"] = sub(content)
    elif isinstance(content, list):
        out["content"] = [
            ({**p, "text": sub(p["text"])} if _is_text_part(p) else p) for p in content
        ]
    return out


def _is_text_part(part: Any) -> bool:
    return isinstance(part, dict) and isinstance(part.get("text"), str)


# --------------------------------------------------------------------------- rules


def keyword_deny(
    words: Iterable[str],
    *,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str | None = None,
    ignore_case: bool = True,
) -> Guardrail:
    """Trip when any of ``words`` appears in the payload (substring match, case-insensitive by
    default). ``action="redact"`` scrubs the matches to ``[redacted]`` instead of blocking."""
    terms = [w for w in words if w]
    flags = re.IGNORECASE if ignore_case else 0
    pattern = re.compile("|".join(re.escape(w) for w in terms), flags) if terms else None

    def check(payload: Any, ctx: Context) -> Verdict | None:
        if pattern is None:
            return None
        match = pattern.search(_payload_text(payload))
        if match is None:
            return None
        reason = f"denied keyword: {match.group(0)!r}"
        if action == "redact":
            return Verdict(
                "redact",
                reason=reason,
                replacement=_redact_payload(payload, lambda s: pattern.sub("[redacted]", s)),
            )
        return Verdict(action, reason=reason)

    return Guardrail(name=name or "keyword_deny", stages=normalize_stages(stage), check=check)


def regex_rule(
    pattern: str | re.Pattern[str],
    *,
    action: str = "flag",
    stage: str | tuple[str, ...] = "input",
    name: str | None = None,
    replacement: str = "[redacted]",
    flags: int = 0,
) -> Guardrail:
    """Trip when ``pattern`` matches the payload text. ``action="redact"`` substitutes each match
    with ``replacement`` and continues."""
    rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

    def check(payload: Any, ctx: Context) -> Verdict | None:
        if rx.search(_payload_text(payload)) is None:
            return None
        reason = f"matched /{rx.pattern}/"
        if action == "redact":
            return Verdict(
                "redact",
                reason=reason,
                replacement=_redact_payload(payload, lambda s: rx.sub(replacement, s)),
            )
        return Verdict(action, reason=reason)

    return Guardrail(name=name or "regex_rule", stages=normalize_stages(stage), check=check)


def _spotlight_delimiters(delimiter: str) -> tuple[str, str]:
    """Derive the (opening, closing) wrapper from ``delimiter``. A tag-shaped delimiter
    (``"<untrusted>"``) yields a matching close tag (``"</untrusted>"``); any other string is used
    verbatim on both sides (``"###"`` → ``"###"`` … ``"###"``)."""
    d = delimiter.strip()
    if d.startswith("<") and d.endswith(">") and not d.startswith("</"):
        tag = d[1:-1].strip()
        if tag:
            return f"<{tag}>", f"</{tag}>"
    return delimiter, delimiter


def _spotlight_wrap(text: str, delimiter: str, encode: bool) -> str:
    """Wrap ``text`` in the trust-lowering ``delimiter`` (optionally base-64-encoding the body like
    Azure's spotlighting). Empty/whitespace-only text is returned unchanged (nothing to wrap)."""
    if not text.strip():
        return text
    body = base64.b64encode(text.encode("utf-8")).decode("ascii") if encode else text
    open_tag, close_tag = _spotlight_delimiters(delimiter)
    return f"{open_tag}\n{body}\n{close_tag}"


def spotlight(
    *,
    stage: str | tuple[str, ...] = ("input", "tool_output"),
    delimiter: str = "<untrusted>",
    encode: bool = False,
    name: str = "spotlight",
) -> Guardrail:
    """Wrap untrusted content in a trust-lowering delimiter — a deterministic, ``$0``, offline
    **mitigation** (not a detector), inspired by Azure Foundry's *Spotlighting*.

    The check **always** returns ``Verdict("redact", …)`` — it never blocks; it rewrites the
    payload, wrapping each scannable text field in ``delimiter`` (a tag like ``"<untrusted>"`` gets
    a matching ``"</untrusted>"`` close; any other string is used on both sides) so a model treats
    that span as lower-trust data, not instructions. With ``encode=True`` the wrapped body is
    base-64-encoded (mirroring Azure), further separating data from instructions. Payload shape is
    preserved (string, chat-message list, or dict), so it composes with the deterministic rules that
    follow it and with a bring-your-own judge.

    Most useful at ``tool_output`` (retrieved docs, tool results, emails — the indirect-injection
    surface); also ``input`` when a caller wants to mark the whole turn as untrusted.

    **Honest limits (straight from Azure's own page):** spotlighting is a *mitigation, not
    detection* — it lowers trust, it does not catch an attack. ``encode=True`` **inflates token
    count** (higher model cost, and large docs can exceed context limits), and some models mention
    the encoding in their reply. ``encode`` defaults **off** to avoid silent token inflation. It is
    not an ML or network call.
    """

    def check(payload: Any, ctx: Context) -> Verdict | None:
        wrapped = _redact_payload(payload, lambda s: _spotlight_wrap(s, delimiter, encode))
        return Verdict(
            "redact",
            reason="spotlighted untrusted content",
            replacement=wrapped,
            metadata={"redacted": True},
        )

    return Guardrail(name=name, stages=normalize_stages(stage), check=check)


_URL_RE = re.compile(r"https?://[^\s<>)\]}\"']+", re.IGNORECASE)


def _iter_hosts(text: str) -> Iterator[str]:
    for match in _URL_RE.finditer(text):
        host = (urlsplit(match.group(0)).hostname or "").lower()
        if host:
            yield host


def _host_matches(host: str, domain: str) -> bool:
    """A host matches a domain when it equals it or is a subdomain of it."""
    d = domain.lower().lstrip(".")
    return host == d or host.endswith("." + d)


def url_allowlist(
    domains: Iterable[str],
    *,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str | None = None,
) -> Guardrail:
    """Trip when the payload contains a URL whose host is **not** on ``domains`` (or a subdomain of
    one). Use to keep an agent from being steered to arbitrary sites."""
    allowed = [d for d in domains if d]

    def check(payload: Any, ctx: Context) -> Verdict | None:
        for host in _iter_hosts(_payload_text(payload)):
            if not any(_host_matches(host, d) for d in allowed):
                return Verdict(action, reason=f"URL host not allowlisted: {host}")
        return None

    return Guardrail(name=name or "url_allowlist", stages=normalize_stages(stage), check=check)


def url_deny(
    domains: Iterable[str],
    *,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str | None = None,
) -> Guardrail:
    """Trip when the payload contains a URL whose host is on ``domains`` (or a subdomain of one)."""
    denied = [d for d in domains if d]

    def check(payload: Any, ctx: Context) -> Verdict | None:
        for host in _iter_hosts(_payload_text(payload)):
            if any(_host_matches(host, d) for d in denied):
                return Verdict(action, reason=f"URL host denied: {host}")
        return None

    return Guardrail(name=name or "url_deny", stages=normalize_stages(stage), check=check)


def length_bounds(
    *,
    max_chars: int | None = None,
    max_tokens: int | None = None,
    model: str = "gpt-4o",
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str | None = None,
) -> Guardrail:
    """Trip when the payload exceeds ``max_chars`` and/or ``max_tokens``. Token counts use
    ``cendor.core.tokens`` (exact for OpenAI with tiktoken), so ``max_tokens`` is a real budget."""
    if max_chars is None and max_tokens is None:
        raise ValueError("length_bounds needs at least one of max_chars / max_tokens")

    def check(payload: Any, ctx: Context) -> Verdict | None:
        text = _payload_text(payload)
        if max_chars is not None and len(text) > max_chars:
            return Verdict(action, reason=f"{len(text)} chars exceeds max {max_chars}")
        if max_tokens is not None:
            # count on the message list when we have one (adds per-message framing), else the text
            countable = payload if isinstance(payload, list) else text
            n = tokens.count(countable, model)
            if n > max_tokens:
                return Verdict(action, reason=f"{n} tokens exceeds max {max_tokens}")
        return None

    return Guardrail(name=name or "length_bounds", stages=normalize_stages(stage), check=check)


def json_schema(
    schema: dict,
    *,
    stage: str | tuple[str, ...] = "output",
    action: str = "block",
    name: str | None = None,
) -> Guardrail:
    """Validate structured output against a (minimal) JSON Schema — ``type`` / ``required`` /
    ``properties`` / ``items``, recursively. Trips when the payload is not valid JSON or violates
    the schema, *before* the caller parses it. Pass the model's raw text or an already-parsed
    object.

    This is a deliberately small validator (no ``jsonschema`` dependency — no heavy deps). It does
    not implement the full spec (no ``$ref``, ``oneOf``, ``pattern``, …); for richer validation,
    parse and validate with your own model layer in a :func:`custom` check.
    """

    def check(payload: Any, ctx: Context) -> Verdict | None:
        if isinstance(payload, dict | list):
            data: Any = payload
        else:
            text = _payload_text(payload)
            try:
                data = json.loads(text)
            except (ValueError, TypeError) as exc:
                return Verdict(action, reason=f"output is not valid JSON: {exc}")
        error = _validate(data, schema, "$")
        if error is not None:
            return Verdict(action, reason=f"schema violation: {error}")
        return None

    return Guardrail(name=name or "json_schema", stages=normalize_stages(stage), check=check)


def _resolve_on_error(action: str, on_error: str | None) -> str:
    """Default the error policy from the guardrail's action: a ``block`` gate fails **closed** (an
    errored check is treated as a block), while a ``flag`` degrades to advisory (``fail_open``).
    An explicit ``on_error`` always wins."""
    if on_error is not None:
        return on_error
    return "fail_open" if action == "flag" else "fail_closed"


def custom(
    fn: Callable[[Any, Context], Verdict | None],
    *,
    stage: str | tuple[str, ...] = "input",
    name: str | None = None,
    timeout: float | None = None,
    on_error: str = "fail_closed",
) -> Guardrail:
    """Wrap any ``fn(payload, ctx) -> Verdict | None`` as a :class:`Guardrail` (sync or async).

    Your ``fn`` is arbitrary code, so it can raise or hang: ``timeout`` (seconds) bounds it and
    ``on_error`` (``"fail_closed"`` default / ``"fail_open"``) decides what a raise or timeout does
    — either way the failure is recorded as a decision. See :class:`Guardrail`.
    """
    return Guardrail(
        name=name or str(getattr(fn, "__name__", "custom")),
        stages=normalize_stages(stage),
        check=fn,
        timeout=timeout,
        on_error=on_error,
    )


def llm_judge(
    judge: Callable[[Any, Context], Verdict | bool | None],
    *,
    stage: str | tuple[str, ...] = "output",
    action: str = "block",
    name: str = "llm_judge",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Adapter **contract** for a bring-your-own model judge — not a built-in classifier.

    ``judge`` is *your* callable (sync or ``async``) that makes whatever model call you want and
    returns a :class:`Verdict` (full control over action/reason), ``True`` to trip with the default
    ``action``, or ``None``/``False`` to pass. cendor ships no model here: an extra LLM call costs
    real tokens and adds real latency (typically seconds — far more than the microsecond
    deterministic rules), and a judge is only as good as its prompt. Measure and state that cost
    honestly. See docs/guardrails.md "Honest limits".

    Because a judge is a network call it can hang or fail: ``timeout`` (seconds) bounds it, and
    ``on_error`` sets the fail policy — defaulting to ``fail_closed`` for the ``block`` action
    (a judge outage must not silently open the gate) and ``fail_open`` for a ``flag`` (advisory).
    Compose the check with :mod:`cendor.guardrails.judge` (verdict prompt + strict-JSON parsing).
    """

    def check(payload: Any, ctx: Context) -> Verdict | None:
        return _coerce_judgement(judge(payload, ctx), action)

    async def acheck(payload: Any, ctx: Context) -> Verdict | None:
        result = judge(payload, ctx)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        return _coerce_judgement(result, action)

    import inspect

    chosen = acheck if inspect.iscoroutinefunction(judge) else check
    return Guardrail(
        name=name,
        stages=normalize_stages(stage),
        check=chosen,
        timeout=timeout,
        on_error=_resolve_on_error(action, on_error),
    )


def _coerce_judgement(result: Any, action: str) -> Verdict | None:
    if result is None or result is False:
        return None
    if isinstance(result, Verdict):
        return result
    if result is True:
        return Verdict(action, reason="llm_judge tripped")
    if isinstance(result, str):  # a reason string ⇒ trip with the default action
        return Verdict(action, reason=result)
    return Verdict(action, reason="llm_judge tripped")


# --------------------------------------------------------------------------- minimal JSON Schema

_TYPE_CHECK: dict[str, Callable[[Any], bool]] = {
    "object": lambda d: isinstance(d, dict),
    "array": lambda d: isinstance(d, list),
    "string": lambda d: isinstance(d, str),
    "number": lambda d: isinstance(d, int | float) and not isinstance(d, bool),
    "integer": lambda d: isinstance(d, int) and not isinstance(d, bool),
    "boolean": lambda d: isinstance(d, bool),
    "null": lambda d: d is None,
}


def _validate(data: Any, schema: dict, path: str) -> str | None:
    """Return the first schema violation as a ``$.path: message`` string, or ``None`` if valid."""
    expected = schema.get("type")
    if expected is not None:
        checker = _TYPE_CHECK.get(expected)
        if checker is not None and not checker(data):
            return f"{path}: expected {expected}, got {type(data).__name__}"
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                return f"{path}: missing required key {key!r}"
        for key, subschema in schema.get("properties", {}).items():
            if key in data and isinstance(subschema, dict):
                error = _validate(data[key], subschema, f"{path}.{key}")
                if error is not None:
                    return error
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(data):
            error = _validate(item, schema["items"], f"{path}[{i}]")
            if error is not None:
                return error
    return None


# Opt-in detection-tier adapters (classifier / prompt_guard / language / openai_moderation + the
# three hosted rails) live in .adapters, and the similarity checks (groundedness / denied_topics) in
# .semantic — each rides a BYO dependency, client, or embedding fn, never a hard dep. Re-exported
# here so they read as `rules.classifier` / `rules.bedrock_guardrail` / `rules.groundedness` etc.
# alongside the deterministic built-ins. Imported at the bottom because both depend on the helpers
# above (_payload_text / custom).
from .adapters import (  # noqa: E402  (bottom import breaks the rules↔adapters cycle)
    azure_content_safety,
    bedrock_guardrail,
    classifier,
    language,
    model_armor,
    openai_moderation,
    prompt_guard,
)
from .semantic import denied_topics, groundedness  # noqa: E402

__all__ = [  # noqa: F822 - names are the module's public factories
    "keyword_deny",
    "regex_rule",
    "spotlight",
    "url_allowlist",
    "url_deny",
    "length_bounds",
    "json_schema",
    "custom",
    "llm_judge",
    "classifier",
    "prompt_guard",
    "language",
    "openai_moderation",
    "bedrock_guardrail",
    "azure_content_safety",
    "model_armor",
    "groundedness",
    "denied_topics",
]
