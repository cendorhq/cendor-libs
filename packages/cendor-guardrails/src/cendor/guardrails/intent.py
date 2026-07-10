"""Pre-LLM **intent screening** — should this request reach the model at all, and is it on-topic?

A first-class gate for the question every app asks but no local-first guardrail packages: *what does
the user want, and do we serve that?* It is agent-loop-native and, unlike Azure (which keeps intent
in a separate AI Language service), it lives right in the gate. Three tiered backends, all built on
machinery already here — pick one:

* **embedding exemplars** (local, ``$0`` after a one-time model pull): give a few example phrases
per
  intent; the nearest intent by cosine decides. Pair with
  :func:`cendor.guardrails.embeddings.local_embedder`.
* **bring-your-own classifier** (``classify(text) -> label | {label: score}``): the door for a
  trained intent model (a CLU-style classifier, an ONNX head).
* **bring-your-own small-LLM judge**: build the check with
:func:`cendor.guardrails.judge.intent_prompt`
  + ``rules.llm_judge`` — the Haiku-pre-screen pattern, its own spend budgeted + audited.

Two modes: ``mode="deny"`` trips when the request **matches** an intent (block topics you never
serve); ``mode="allow"`` trips when it matches **none** (an off-topic gate — a support bot answering
only support questions). Defaults to ``action="flag"`` — a heuristic/judge signal is softer than a
literal match; calibrate before you ``block``. **No accuracy claim, no bundled intent taxonomy.**

Imports only :mod:`.decision` (the text helper is lazy), so it re-exports cleanly via :mod:`.rules`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .decision import Action, Context, Guardrail, Verdict, normalize_stages

__all__ = ["intent"]

Embed = Callable[[str], Sequence[float]]
Classify = Callable[[str], Any]
MODES: tuple[str, ...] = ("deny", "allow")


def _text(payload: Any) -> str:
    from .rules import _payload_text

    return _payload_text(payload)


def _resolve_on_error(action: str, on_error: str | None) -> str:
    if on_error is not None:
        return on_error
    return "fail_open" if action == "flag" else "fail_closed"


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _classified_label(result: Any) -> tuple[str, float]:
    """Normalise a classifier result to ``(label, score)``: a bare string label ⇒ score 1.0; a
    ``{label: score}`` mapping ⇒ its argmax."""
    if isinstance(result, str):
        return (result, 1.0)
    if isinstance(result, Mapping):
        if not result:
            return ("", 0.0)
        label = max(result, key=lambda k: float(result[k]))
        return (str(label), float(result[label]))
    raise TypeError(
        f"intent classify() returned {type(result).__name__}; expected a label str or {{label: "
        "score}} mapping"
    )


def _embed_intents(intents: Any) -> dict[str, list[str]]:
    """The embedding backend needs a ``{label: [exemplars]}`` mapping (a single string exemplar is
    accepted and wrapped)."""
    if not isinstance(intents, Mapping):
        raise TypeError(
            "intent(embed=…) needs intents as a {label: [example, …]} mapping, got "
            f"{type(intents).__name__}"
        )
    out: dict[str, list[str]] = {}
    for label, examples in intents.items():
        out[str(label)] = [examples] if isinstance(examples, str) else [str(e) for e in examples]
    return out


def _label_set(intents: Any) -> set[str]:
    """The classifier backend scopes ``intents`` to a set of label names (mapping keys or a plain
    collection of labels)."""
    if isinstance(intents, Mapping):
        return {str(k) for k in intents}
    if isinstance(intents, str):
        return {intents}
    return {str(x) for x in intents}


def intent(
    intents: Mapping[str, Sequence[str]] | Sequence[str],
    *,
    embed: Embed | None = None,
    classify: Classify | None = None,
    mode: str = "deny",
    threshold: float = 0.8,
    stage: str | tuple[str, ...] = "input",
    action: Action = "flag",
    name: str = "intent",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Screen a request by **intent** before the model runs. Provide exactly one backend —
    ``embed=`` (semantic exemplars) or ``classify=`` (a BYO label classifier). For the LLM-judge
    backend, use :func:`cendor.guardrails.judge.intent_prompt` with ``rules.llm_judge`` instead.

    ```python
    from cendor.guardrails import rules, embeddings
    embed = embeddings.local_embedder()
    gate = [rules.intent({"support": ["reset my password"]}, embed=embed, mode="allow")]
    ```

    Args:
        intents: For ``embed=``, a ``{label: [example phrase, …]}`` mapping. For ``classify=``, the
            in-scope label names (a mapping's keys, or a plain list of labels).
        mode: ``"deny"`` trips when the request matches an intent (score ``>= threshold`` for the
            nearest, and — for ``classify`` — that label is in ``intents``); ``"allow"`` trips when
            it matches **none** (off-topic). The decision records ``metadata["intent"]`` (the
            closest
            label) and ``metadata["score"]``.
        threshold: Cosine (``embed``) or classifier-score cutoff for "matched".
        action: ``"flag"`` (default, advisory), ``"redact"``, or ``"block"``.

    The embedding backend embeds every exemplar once on first check (construction makes no call).
    There is **no accuracy claim** and no shipped taxonomy — this is a screening heuristic;
    calibrate
    ``threshold`` (and prefer ``flag`` until you have) before you ``block``.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; must be one of {MODES}")
    if (embed is None) == (classify is None):
        raise ValueError("intent() needs exactly one of embed= or classify=")

    if embed is not None:
        check = _embedding_check(_embed_intents(intents), embed, mode, threshold, action)
    else:
        assert classify is not None
        check = _classifier_check(_label_set(intents), classify, mode, threshold, action)

    return Guardrail(
        name=name,
        stages=normalize_stages(stage),
        check=check,
        timeout=timeout,
        on_error=_resolve_on_error(action, on_error),
    )


def _embedding_check(
    intents: dict[str, list[str]], embed: Embed, mode: str, threshold: float, action: str
) -> Callable[[Any, Context], Verdict | None]:
    cache: dict[str, list[tuple[str, Sequence[float]]]] = {}

    def _vectors() -> list[tuple[str, Sequence[float]]]:
        if "v" not in cache:
            cache["v"] = [
                (label, embed(example))
                for label, examples in intents.items()
                for example in examples
            ]
        return cache["v"]

    def check(payload: Any, ctx: Context) -> Verdict | None:
        vecs = _vectors()
        if not vecs:
            return None
        query = embed(_text(payload))
        best_label, best_score = "", -1.0
        for label, vec in vecs:
            sim = _cosine(query, vec)
            if sim > best_score:
                best_label, best_score = label, sim
        matched = best_score >= threshold
        return _verdict(mode, action, matched, best_label, best_score, threshold)

    return check


def _classifier_check(
    labels: set[str], classify: Classify, mode: str, threshold: float, action: str
) -> Callable[[Any, Context], Verdict | None]:
    def check(payload: Any, ctx: Context) -> Verdict | None:
        label, score = _classified_label(classify(_text(payload)))
        matched = score >= threshold and label in labels
        return _verdict(mode, action, matched, label, score, threshold)

    return check


def _verdict(
    mode: str, action: str, matched: bool, label: str, score: float, threshold: float
) -> Verdict | None:
    meta = {"intent": label, "score": round(float(score), 4)}
    if mode == "deny":
        if not matched:
            return None
        return Verdict(
            action, reason=f"denied intent {label!r}: {score:.2f} >= {threshold}", metadata=meta
        )
    # mode == "allow": trip when nothing in scope matched (off-topic)
    if matched:
        return None
    detail = f"closest {label!r} {score:.2f}" if label else "no intent matched"
    return Verdict(action, reason=f"off-topic ({detail} < {threshold})", metadata=meta)
