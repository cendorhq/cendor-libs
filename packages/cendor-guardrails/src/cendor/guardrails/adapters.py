"""Opt-in detection-tier adapters — beyond the deterministic tier-0 built-ins in :mod:`.rules`.

These reach past regex/arithmetic to a local ML classifier, a language detector, a hosted moderation
endpoint, and the three **hosted rails** (AWS Bedrock, Azure AI Content Safety, Google Model Armor)
— the detection-tier model in docs/guardrails.md "Threat model". Each rides a **bring-your-own**
dependency or client — never a hard dependency of this package: a classifier callable, an optional
``[promptguard]`` / ``[langid]`` extra (lazy-imported), or a provider client you pass in. They are
re-exported through :mod:`.rules` (``rules.classifier`` / ``rules.prompt_guard`` /
``rules.language`` / ``rules.openai_moderation`` / ``rules.bedrock_guardrail`` /
``rules.azure_content_safety`` / ``rules.model_armor``).

**Cloud check, local evidence.** The hosted rails call *your* cloud account (metered, priced by the
vendor — the base package stays local-first and free). But the verdict still runs through the same
engine: every trip or flag emits a local :class:`~cendor.guardrails.GuardrailDecision` on the
``cendor.core`` bus, so ``acttrace`` chains it as tamper-evident evidence exactly like a
deterministic rule. The reason records only *which* cloud policy fired — never the raw payload.

**Honest claims.** There is **no jailbreak-detection claim** anywhere here. :func:`prompt_guard` is
exactly what its name says — an *adapter* around a prompt-injection classifier **you** download;
reproduce its public eval (``benchmarks/eval_promptguard.py``) and publish the numbers before citing
any detection rate. The hosted rails are adapters over *vendor* services — cite the vendor's own
pricing/accuracy pages, never a number invented here. Classifiers and filters are beaten by
mutation/obfuscation attacks — layer them, don't trust one. See docs/guardrails.md "Threat model".

This module imports only :mod:`.decision` (the text helper is imported lazily) so it never forms an
import cycle with :mod:`.rules`, which re-exports these factories. The cloud clients are
**duck-typed** — nothing here imports boto3 / azure-ai-contentsafety / google-cloud-modelarmor; you
construct the client and pass it in (the optional ``[bedrock]`` / ``[azure]`` / ``[modelarmor]``
extras just install the respective SDK for convenience).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .decision import Context, Guardrail, Verdict, normalize_stages

__all__ = [
    "classifier",
    "prompt_guard",
    "language",
    "openai_moderation",
    "bedrock_guardrail",
    "azure_content_safety",
    "model_armor",
]


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


def _annotation(**keys: Any) -> dict[str, Any]:
    """Build a :attr:`Verdict.metadata` dict from the **reserved annotation keys** (``severity`` /
    ``detected`` / ``filtered`` / ``redacted`` / ``citation`` / ``license`` — documented in
    ``docs/specs/bus-events.md``), dropping any that are ``None``. Adapters use it so a vendor's
    detected/severity signal rides the decision's metadata into the acttrace chain — no shape
    change, no acttrace edit."""
    return {k: v for k, v in keys.items() if v is not None}


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
        ann = _annotation(detected=True, filtered=action != "flag")
        if cats is not None:
            hit = sorted(c for c in flagged_names if c.lower() in cats)
            if not hit:
                return None
            return Verdict(action, reason=f"moderation flagged: {', '.join(hit)}", metadata=ann)
        if _get(result, "flagged", False):
            names = ", ".join(flagged_names) or "policy"
            return Verdict(action, reason=f"moderation flagged: {names}", metadata=ann)
        return None

    return _mk(check, name=name, stage=stage, timeout=timeout, action=action, on_error=on_error)


# --------------------------------------------------------------------------- hosted rails
#
# Each of the three below calls *your* cloud account (metered — see the vendor's own pricing page,
# cited in docs/guardrails.md; the base package stays local-first and free) and turns the vendor's
# verdict into a local GuardrailDecision → acttrace evidence: **cloud check, local evidence.** The
# clients are duck-typed (no boto3 / azure / google import here); build them yourself. On the input
# and tool_call stages the source is the user/tool text; on the output and tool_output stages it is
# the model/tool text — the adapters pick the right direction from ``ctx.stage`` where the API
# distinguishes them.


def _uniq(items: Any) -> list[str]:
    """Order-preserving de-dupe for reason labels."""
    seen: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.append(x)
    return seen


def bedrock_guardrail(
    client: Any,
    guardrail_id: str,
    *,
    guardrail_version: str = "DRAFT",
    source: str | None = None,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str = "bedrock_guardrail",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """AWS Bedrock **``ApplyGuardrail``** as a guardrail — the flagship hosted rail: it evaluates
    any text against your pre-configured Bedrock guardrail **independently of any model** (AWS:
    "assess any text … without invoking the foundation models"), so it works no matter which
    provider your agent uses.

    ``client`` is *your* ``boto3.client("bedrock-runtime")`` (needs AWS credentials + a configured
    guardrail); ``guardrail_id`` / ``guardrail_version`` identify it. This calls
    ``client.apply_guardrail(...)`` with ``source`` = ``"INPUT"`` on the input/tool_call stages and
    ``"OUTPUT"`` on the output/tool_output stages (override with ``source=``). It trips when the
    response ``action`` is ``"GUARDRAIL_INTERVENED"``; the reason records the top-level
    ``actionReason`` (or the policy labels that fired) — never the payload. With ``action="redact"``
    and a masked ``outputs[].text`` in the response (Bedrock's PII masking), the masked text becomes
    the replacement — best used on a **string** payload / the output stage.

    Metered per text unit (AWS Bedrock pricing; some filters are free). It is a network call — set
    ``timeout`` / ``on_error`` (fail-closed by default for a block gate). The ``[bedrock]`` extra
    installs boto3 for convenience.
    """

    def check(payload: Any, ctx: Context) -> Verdict | None:
        src = source or ("OUTPUT" if ctx.stage in ("output", "tool_output") else "INPUT")
        resp = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source=src,
            content=[{"text": {"text": _text(payload)}}],
        )
        if _get(resp, "action") != "GUARDRAIL_INTERVENED":
            return None
        reason = _bedrock_reason(resp)
        if action == "redact":
            masked = _bedrock_masked(resp)
            if masked is not None:
                return Verdict(
                    "redact",
                    reason=reason,
                    replacement=masked,
                    metadata=_annotation(detected=True, filtered=True, redacted=True),
                )
        return Verdict(action, reason=reason, metadata=_annotation(detected=True, filtered=True))

    return _mk(check, name=name, stage=stage, timeout=timeout, action=action, on_error=on_error)


def _bedrock_reason(resp: Any) -> str:
    reason = _get(resp, "actionReason")
    if isinstance(reason, str) and reason:
        return f"Bedrock guardrail intervened: {reason}"
    labels = _bedrock_assessment_labels(resp)
    return f"Bedrock guardrail intervened: {', '.join(labels) if labels else 'policy'}"


def _bedrock_assessment_labels(resp: Any) -> list[str]:
    """Human-readable labels of *what* fired — category names/types only, not the matched value."""
    labels: list[str] = []
    for a in _get(resp, "assessments") or []:
        for t in _get(_get(a, "topicPolicy"), "topics") or []:
            labels.append(f"topic:{_get(t, 'name')}" if _get(t, "name") else "")
        for f in _get(_get(a, "contentPolicy"), "filters") or []:
            labels.append(f"content:{_get(f, 'type')}" if _get(f, "type") else "")
        sp = _get(a, "sensitiveInformationPolicy")
        for e in _get(sp, "piiEntities") or []:
            labels.append(f"pii:{_get(e, 'type')}" if _get(e, "type") else "")
        for r in _get(sp, "regexes") or []:
            labels.append(f"regex:{_get(r, 'name')}" if _get(r, "name") else "")
        wp = _get(a, "wordPolicy")
        if _get(wp, "customWords"):
            labels.append("word:custom")
        for m in _get(wp, "managedWordLists") or []:
            labels.append(f"word:{_get(m, 'type')}" if _get(m, "type") else "")
    return _uniq(labels)


def _bedrock_masked(resp: Any) -> str | None:
    for o in _get(resp, "outputs") or []:
        t = _get(o, "text")
        if isinstance(t, str) and t:
            return t
    return None


def azure_content_safety(
    client: Any,
    *,
    documents: list[str] | tuple[str, ...] | None = None,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str = "azure_content_safety",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Azure AI Content Safety **Prompt Shields** as a guardrail — detects user-prompt and document
    injection/jailbreak attacks.

    ``client`` is *your* ``azure.ai.contentsafety.ContentSafetyClient`` (needs an endpoint + key or
    Entra ID). This calls ``client.shield_prompt(options={"userPrompt": <text>, "documents":
    [...]})`` and trips when the response's ``userPromptAnalysis.attackDetected`` (or any
    ``documentsAnalysis[].attackDetected``) is true — the binary Prompt Shields signal (there is no
    severity/redaction to remap, so ``block`` / ``flag`` are the meaningful actions). If your SDK
    version spells the call differently, wrap it in :func:`~cendor.guardrails.rules.custom` instead.

    Metered per text record (Azure AI Content Safety pricing; F0 free tier available). It is a
    network call — set ``timeout`` / ``on_error``. The ``[azure]`` extra installs
    azure-ai-contentsafety.
    """
    docs = list(documents) if documents else []

    def check(payload: Any, ctx: Context) -> Verdict | None:
        resp = client.shield_prompt(options={"userPrompt": _text(payload), "documents": docs})
        hits = _azure_attacks(resp)
        if not hits:
            return None
        return Verdict(
            action,
            reason=f"Azure Prompt Shields: attack detected ({', '.join(hits)})",
            metadata=_annotation(detected=True, filtered=action != "flag"),
        )

    return _mk(check, name=name, stage=stage, timeout=timeout, action=action, on_error=on_error)


def _azure_attacks(resp: Any) -> list[str]:
    """Read Prompt Shields' binary signals across camelCase (REST) and snake_case (SDK) shapes."""
    hits: list[str] = []
    upa = _get(resp, "userPromptAnalysis")
    if upa is None:
        upa = _get(resp, "user_prompt_analysis")
    if upa is not None and (_get(upa, "attackDetected") or _get(upa, "attack_detected")):
        hits.append("user prompt")
    da = _get(resp, "documentsAnalysis")
    if da is None:
        da = _get(resp, "documents_analysis")
    for i, d in enumerate(da or []):
        if _get(d, "attackDetected") or _get(d, "attack_detected"):
            hits.append(f"document[{i}]")
    return hits


def model_armor(
    client: Any,
    template: str,
    *,
    stage: str | tuple[str, ...] = "input",
    action: str = "block",
    name: str = "model_armor",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Google Cloud **Model Armor** as a guardrail — screens prompts and responses against a
    template (prompt-injection & jailbreak, Sensitive Data Protection, malicious URIs,
    responsible-AI filters).

    ``client`` is *your* ``google.cloud.modelarmor_v1.ModelArmorClient`` (needs GCP credentials + a
    regional endpoint); ``template`` is the full resource path
    ``projects/{project}/locations/{location}/templates/{template}``. On the input/tool_call stages
    this calls ``sanitize_user_prompt``; on output/tool_output, ``sanitize_model_response``
    (both with a plain dict request — no google types imported). It trips when
    ``sanitization_result.filter_match_state`` is ``MATCH_FOUND``; the reason lists which filters
    matched — never the payload.

    Metered per token (Model Armor pricing; a monthly free allocation applies). It is a network call
    — set ``timeout`` / ``on_error``. ``[modelarmor]`` extra installs google-cloud-modelarmor.
    """

    def check(payload: Any, ctx: Context) -> Verdict | None:
        text = _text(payload)
        if ctx.stage in ("output", "tool_output"):
            resp = client.sanitize_model_response(
                request={"name": template, "model_response_data": {"text": text}}
            )
        else:
            resp = client.sanitize_user_prompt(
                request={"name": template, "user_prompt_data": {"text": text}}
            )
        matched = _model_armor_matches(resp)
        if not matched:
            return None
        return Verdict(
            action,
            reason=f"Model Armor matched: {', '.join(matched)}",
            metadata=_annotation(detected=True, filtered=action != "flag"),
        )

    return _mk(check, name=name, stage=stage, timeout=timeout, action=action, on_error=on_error)


_MATCH_FOUND = "MATCH_FOUND"


def _match_found(state: Any) -> bool:
    """A Model Armor match state is ``MATCH_FOUND`` (exact — ``NO_MATCH_FOUND`` must not match).
    Handles a proto enum (``.name``), a plain string, or an int-like fallback."""
    name = getattr(state, "name", None)
    if name is None and isinstance(state, str):
        name = state
    return name == _MATCH_FOUND


def _model_armor_matches(resp: Any) -> list[str]:
    sr = _get(resp, "sanitization_result")
    if sr is None:
        sr = _get(resp, "sanitizationResult")
    if sr is None:
        return []
    top = _get(sr, "filter_match_state")
    if top is None:
        top = _get(sr, "filterMatchState")
    if not _match_found(top):
        return []
    fr = _get(sr, "filter_results")
    if fr is None:
        fr = _get(sr, "filterResults")
    if fr is None:
        return ["filter"]
    if isinstance(fr, Mapping):
        items: Any = fr.items()
    else:
        items = [(k, getattr(fr, k)) for k in getattr(fr, "__dict__", {})]
    matched = [str(key) for key, val in items if _contains_match(val)]
    return matched or ["filter"]


def _contains_match(val: Any, depth: int = 0) -> bool:
    """Best-effort recursive scan for a nested ``match_state``/``matchState`` == ``MATCH_FOUND``."""
    if val is None or depth > 6:
        return False
    st = _get(val, "match_state")
    if st is None:
        st = _get(val, "matchState")
    if st is not None and _match_found(st):
        return True
    if isinstance(val, Mapping):
        return any(_contains_match(v, depth + 1) for v in val.values())
    if isinstance(val, (list, tuple)):
        return any(_contains_match(v, depth + 1) for v in val)
    d = getattr(val, "__dict__", None)
    if isinstance(d, dict):
        return any(_contains_match(v, depth + 1) for v in d.values())
    return False
