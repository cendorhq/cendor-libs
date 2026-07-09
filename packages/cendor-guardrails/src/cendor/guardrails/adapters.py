"""Opt-in detection-tier adapters — beyond the deterministic tier-0 built-ins in :mod:`.rules`.

These reach past regex/arithmetic to a local ML classifier, a language detector, and a hosted
moderation endpoint (the detection-tier model in docs/guardrails.md "Threat model"). Each rides a
**bring-your-own** dependency or client — never a hard dependency of this package: a classifier
callable, an optional ``[promptguard]`` / ``[langid]`` extra (lazy-imported), or a provider client
you pass in. They are re-exported through :mod:`.rules` (``rules.classifier`` /
``rules.prompt_guard`` / ``rules.language`` / ``rules.openai_moderation``).

**Honest claims.** There is **no jailbreak-detection claim** anywhere here. :func:`prompt_guard` is
exactly what its name says — an *adapter* around a prompt-injection classifier **you** download;
reproduce its public eval (``benchmarks/eval_promptguard.py``) and publish the numbers before citing
any detection rate. Classifiers are beaten by mutation/obfuscation attacks — layer them, don't trust
one. See docs/guardrails.md "Threat model".

This module imports only :mod:`.decision` (the text helper is imported lazily) so it never forms an
import cycle with :mod:`.rules`, which re-exports these factories.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .decision import Context, Guardrail, Verdict, normalize_stages

__all__ = ["classifier", "prompt_guard", "language", "openai_moderation"]


def _text(payload: Any) -> str:
    """Flatten a payload to scannable text (lazy import keeps this module cycle-free with rules)."""
    from .rules import _payload_text

    return _payload_text(payload)


def _resolve_on_error(action: str, on_error: str | None) -> str:
    if on_error is not None:
        return on_error
    return "fail_open" if action == "flag" else "fail_closed"


def _mk(
    check: Callable[[Any, Context], Verdict | None],
    *,
    name: str,
    stage: Any,
    timeout: Any,
    action: str,
    on_error: str | None,
) -> Guardrail:
    return Guardrail(
        name=name,
        stages=normalize_stages(stage),
        check=check,
        timeout=timeout,
        on_error=_resolve_on_error(action, on_error),
    )


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _score(result: Any, label: str | None, threshold: float) -> tuple[float, bool]:
    """Normalise a classifier result (bool / float / {label: score}) to (score, tripped)."""
    if isinstance(result, bool):
        return (1.0 if result else 0.0, result)
    if isinstance(result, (int, float)):
        s = float(result)
        return (s, s >= threshold)
    if isinstance(result, Mapping):
        if label is not None:
            s = float(result.get(label, 0.0))
        else:
            s = max((float(v) for v in result.values()), default=0.0)
        return (s, s >= threshold)
    raise TypeError(
        f"classifier returned {type(result).__name__}; expected bool, number, or mapping"
    )


def classifier(
    classify: Callable[[str], Any],
    *,
    threshold: float = 0.5,
    label: str | None = None,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str = "classifier",
    reason: str | None = None,
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Wrap a **local classifier** as a guardrail — the generic, license-agnostic contract.

    ``classify(text)`` returns a float score in ``[0, 1]``, a ``{label: score}`` mapping, or a bool.
    The guardrail trips when the (selected ``label``'s, else the max) score ``>= threshold`` (or the
    bool is ``True``). Bring **any** local classifier — an ONNX model, a ``transformers`` pipeline,
    a heuristic. A network call can hang, so set ``timeout`` / ``on_error`` for a remote classifier.
    """

    def check(payload: Any, ctx: Context) -> Verdict | None:
        s, tripped = _score(classify(_text(payload)), label, threshold)
        if not tripped:
            return None
        return Verdict(action, reason=reason or f"{name}: score {s:.2f} >= {threshold}")

    return _mk(check, name=name, stage=stage, timeout=timeout, action=action, on_error=on_error)


def prompt_guard(
    model: str = "meta-llama/Llama-Prompt-Guard-2-86M",
    *,
    threshold: float = 0.5,
    device: Any = None,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str = "prompt_guard",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """A **prompt-injection classifier adapter** — optional ``[promptguard]`` extra, lazy
    ``transformers``.

    Loads ``model`` from Hugging Face at first check; **weights are never bundled**, and you accept
    the model's license to download it — Meta's Llama Prompt Guard 2 is under the **Llama Community
    License** and gated on Hugging Face (base is MIT mDeBERTa). Returns a :func:`classifier`
    guardrail scoring each input for injection likelihood.

    **No jailbreak-detection claim.** This is an adapter around a model *you* supply; reproduce the
    public eval (``benchmarks/eval_promptguard.py``) and publish the numbers before citing a
    detection rate. Classifiers are beaten by mutation attacks — see docs/guardrails.md "Threat
    model". For a non-Meta / ONNX model, use :func:`classifier` directly with your own ``classify``.
    """
    state: dict[str, Any] = {"clf": None}

    def _load() -> Any:
        try:
            from transformers import pipeline  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "prompt_guard needs the optional extra: "
                "pip install 'cendor-guardrails[promptguard]'."
                " It runs a prompt-injection classifier (default Llama-Prompt-Guard-2-86M)"
                " from Hugging Face — accept the model's license to download it; weights are never"
                " bundled. Or pass your own classify() to rules.classifier()."
            ) from exc
        return pipeline("text-classification", model=model, device=device)

    def classify(text: str) -> float:
        if state["clf"] is None:
            state["clf"] = _load()
        rows = state["clf"](text, truncation=True)
        row = rows[0] if isinstance(rows, list) else rows
        lbl = str(_get(row, "label", "")).upper()
        sc = float(_get(row, "score", 0.0))
        # PromptGuard-class models label benign vs injection/malicious; map to an injection score.
        injection = (
            "INJECT" in lbl or "MALICIOUS" in lbl or "JAILBREAK" in lbl or lbl in {"LABEL_1", "1"}
        )
        return sc if injection else 1.0 - sc

    return classifier(
        classify,
        threshold=threshold,
        stage=stage,
        action=action,
        name=name,
        timeout=timeout,
        on_error=on_error,
    )


def language(
    allowed: list[str] | tuple[str, ...],
    *,
    detect: Callable[[str], str] | None = None,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str = "language",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Trip when the payload's detected language is **not** in ``allowed`` (ISO codes) — a guard
    against the language-switch bypass, a documented real-world jailbreak vector.

    ``detect(text) -> str`` is bring-your-own; without it, the optional ``[langid]`` extra provides
    a local detector (``py3langid``, BSD). Language ID on short/mixed text is unreliable — keep this
    advisory (``action="flag"``) unless you control the input distribution.
    """
    allow = {a.lower() for a in allowed}

    def _default_detect(text: str) -> str:
        try:
            import py3langid as langid  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "language() needs a detector: pass detect=..., or install the optional extra: "
                "pip install 'cendor-guardrails[langid]' (adds py3langid)."
            ) from exc
        code, _ = langid.classify(text)
        return str(code)

    det = detect or _default_detect

    def check(payload: Any, ctx: Context) -> Verdict | None:
        text = _text(payload).strip()
        if not text:
            return None
        lang = det(text)
        if lang and lang.lower() not in allow:
            return Verdict(action, reason=f"language {lang!r} not in allowed {sorted(allow)}")
        return None

    return _mk(check, name=name, stage=stage, timeout=timeout, action=action, on_error=on_error)


def _flagged_categories(categories: Any) -> list[str]:
    """The category names an OpenAI moderation result flagged True (dict or pydantic shape)."""
    if isinstance(categories, Mapping):
        items: Any = categories.items()
    else:
        items = [(k, getattr(categories, k)) for k in getattr(categories, "__dict__", {})]
    return sorted(k for k, v in items if v)


def openai_moderation(
    client: Any,
    *,
    model: str = "omni-moderation-latest",
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    categories: list[str] | tuple[str, ...] | None = None,
    name: str = "openai_moderation",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Trip when OpenAI's **free, non-LLM** moderation endpoint flags the payload — the cheapest
    hosted tier.

    ``client`` is *your* OpenAI client (needs a key); this calls ``client.moderations.create(...)``.
    Restrict to specific ``categories`` (e.g. ``["violence", "hate"]``) or trip on any flag. It is a
    network call — bound it with ``timeout`` and pick an ``on_error`` policy (fail-closed by default
    for a block gate). This library stores nothing; the request goes to OpenAI.
    """
    cats = {c.lower() for c in categories} if categories else None

    def check(payload: Any, ctx: Context) -> Verdict | None:
        resp = client.moderations.create(model=model, input=_text(payload))
        results = _get(resp, "results") or []
        if not results:
            return None
        result = results[0]
        flagged_names = _flagged_categories(_get(result, "categories", {}))
        if cats is not None:
            hit = sorted(c for c in flagged_names if c.lower() in cats)
            return Verdict(action, reason=f"moderation flagged: {', '.join(hit)}") if hit else None
        if _get(result, "flagged", False):
            names = ", ".join(flagged_names) or "policy"
            return Verdict(action, reason=f"moderation flagged: {names}")
        return None

    return _mk(check, name=name, stage=stage, timeout=timeout, action=action, on_error=on_error)
