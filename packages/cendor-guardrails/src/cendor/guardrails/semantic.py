"""Similarity-based checks over a **bring-your-own** embedding function — groundedness and
denied-topics. docs/guardrails.md.

Two open-ended risks you can catch with embeddings instead of an LLM judge:

* :func:`groundedness` — trip when a response is **not** close enough to any provided source (a RAG
  hallucination gate: the answer drifted from the retrieved passages).
* :func:`denied_topics` — trip when the payload is **too** close to any denied-topic exemplar
  (steer an agent off subjects you never want it to discuss).

Both take an ``embed(text) -> sequence[float]`` callable — **you** supply the model (a local
sentence-transformer, a hosted embeddings endpoint, anything). cendor ships **no** embedding model,
mirroring ``cassette``'s bring-your-own-scorer precedent. Cosine similarity is computed in pure
Python (no numpy). These are heuristics: a threshold you tune, not a guarantee — keep an ungrounded
answer advisory (``action="flag"``) unless you have measured your own corpus. For richer, reasoned
judgement, use the :mod:`cendor.guardrails.judge` LLM-judge helpers instead.

Imports only :mod:`.decision` (the text helper is lazy) so it re-exports cleanly via :mod:`.rules`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from .decision import Action, Context, Guardrail, Verdict, normalize_stages

__all__ = ["groundedness", "denied_topics", "custom_category"]

Embed = Callable[[str], Sequence[float]]


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


def _lazy_vectors(embed: Embed, texts: Sequence[str]) -> Callable[[], list[Sequence[float]]]:
    """Embed ``texts`` once, on first check (not at construction — so building an agent never makes
    a network call, and a mocked ``embed`` in a test is only invoked when the guardrail runs)."""
    cache: dict[str, list[Sequence[float]]] = {}

    def get() -> list[Sequence[float]]:
        if "v" not in cache:
            cache["v"] = [embed(t) for t in texts]
        return cache["v"]

    return get


def groundedness(
    embed: Embed,
    sources: Sequence[str],
    *,
    threshold: float = 0.75,
    stage: str | tuple[str, ...] = "output",
    action: Action = "flag",
    name: str = "groundedness",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Trip when the payload's max cosine similarity to any of ``sources`` is **below**
    ``threshold`` — i.e. the response is not grounded in the retrieved passages (a RAG
    hallucination gate).

    ```python
    from cendor.guardrails import rules, embeddings
    embed = embeddings.local_embedder()
    gate = [rules.groundedness(embed, sources=["the earth orbits the sun"])]
    ```

    ``embed(text)`` is bring-your-own. ``sources`` (the retrieved passages / knowledge you expect
    the answer based on) are embedded once on first check. Defaults to the ``output`` stage and
    ``action="flag"`` — groundedness is a tuned heuristic, so make it advisory unless you have
    measured your threshold on your own data. Empty ``sources`` never trips (nothing to ground
    against).
    """
    source_vecs = _lazy_vectors(embed, list(sources))

    def check(payload: Any, ctx: Context) -> Verdict | None:
        vecs = source_vecs()
        if not vecs:
            return None
        answer = embed(_text(payload))
        best = max((_cosine(answer, v) for v in vecs), default=0.0)
        if best >= threshold:
            return None
        return Verdict(action, reason=f"ungrounded: max similarity {best:.2f} < {threshold}")

    return Guardrail(
        name=name,
        stages=normalize_stages(stage),
        check=check,
        timeout=timeout,
        on_error=_resolve_on_error(action, on_error),
    )


def custom_category(
    category: str,
    examples: Sequence[str],
    *,
    embed: Embed,
    threshold: float = 0.8,
    stage: str | tuple[str, ...] = "input",
    action: Action = "flag",
    name: str | None = None,
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Trip when the payload is semantically close to a **custom category** you define by example —
    the local, ``$0`` counterpart to Azure Content Safety's *rapid custom categories* (description +
    examples → embedding search), with no cloud call and no training step.

    ```python
    from cendor.guardrails import rules, embeddings
    embed = embeddings.local_embedder()
    gate = [rules.custom_category("code", ["write a program"], embed=embed, action="flag")]
    ```

    ``category`` is the label recorded on the decision (``metadata["category"]``); ``examples`` are
    a few exemplar phrases of the category (embedded once on first check). ``embed(text)`` is
    **bring-your-own** — pass :func:`cendor.guardrails.embeddings.local_embedder` (the
    ``[embeddings]`` extra, model2vec, offline) or any embeddings endpoint. The check trips when the
    payload's max cosine similarity to any example is **at or above** ``threshold``, recording the
    closest example's score in ``metadata["score"]``. This catches paraphrases a
    :func:`keyword_deny` misses (e.g. a ``"code_requests"`` category defined by ``["write a
    program", "build an app"]`` fires on *"create a hello-world app"*).

    Defaults to ``action="flag"`` — a similarity threshold is a tuned heuristic, so keep it advisory
    until you have calibrated it on your own inputs; switch to ``block`` once measured. There is
    **no catch-rate claim**: a benchmark on a named corpus opens that gate. Empty ``examples`` never
    trips.
    """
    example_vecs = _lazy_vectors(embed, list(examples))

    def check(payload: Any, ctx: Context) -> Verdict | None:
        vecs = example_vecs()
        if not vecs:
            return None
        query = embed(_text(payload))
        sims = [_cosine(query, v) for v in vecs]
        best_i = max(range(len(sims)), key=sims.__getitem__)
        best = sims[best_i]
        if best < threshold:
            return None
        return Verdict(
            action,
            reason=f"custom category {category!r}: sim {best:.2f} >= {threshold}",
            metadata={"category": category, "score": round(best, 4)},
        )

    return Guardrail(
        name=name or f"custom_category:{category}",
        stages=normalize_stages(stage),
        check=check,
        timeout=timeout,
        on_error=_resolve_on_error(action, on_error),
    )


def denied_topics(
    embed: Embed,
    topics: Sequence[str],
    *,
    threshold: float = 0.8,
    stage: str | tuple[str, ...] = "input",
    action: Action = "block",
    name: str = "denied_topics",
    timeout: float | None = None,
    on_error: str | None = None,
) -> Guardrail:
    """Trip when the payload's max cosine similarity to any denied-topic exemplar is **at or above**
    ``threshold`` — steer an agent off subjects it must never engage.

    ```python
    from cendor.guardrails import rules, embeddings
    embed = embeddings.local_embedder()
    gate = [rules.denied_topics(embed, ["medical diagnosis", "legal advice"])]
    ```

    ``embed(text)`` is bring-your-own; ``topics`` are short exemplar phrases of what to refuse (e.g.
    ``["medical diagnosis", "legal advice"]``), embedded once on first check. The reason names the
    closest topic and the similarity — never the payload. A semantic match catches paraphrases a
    ``keyword_deny`` misses, but it is a tuned heuristic: calibrate ``threshold`` on your inputs.
    """
    topic_vecs = _lazy_vectors(embed, list(topics))
    topic_list = list(topics)

    def check(payload: Any, ctx: Context) -> Verdict | None:
        vecs = topic_vecs()
        if not vecs:
            return None
        query = embed(_text(payload))
        sims = [_cosine(query, v) for v in vecs]
        best_i = max(range(len(sims)), key=sims.__getitem__)
        if sims[best_i] < threshold:
            return None
        return Verdict(
            action,
            reason=f"denied topic {topic_list[best_i]!r}: sim {sims[best_i]:.2f} >= {threshold}",
        )

    return Guardrail(
        name=name,
        stages=normalize_stages(stage),
        check=check,
        timeout=timeout,
        on_error=_resolve_on_error(action, on_error),
    )
