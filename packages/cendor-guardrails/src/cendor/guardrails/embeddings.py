"""A free, offline embedding function for the semantic checks — the zero-config ignition for
:func:`~cendor.guardrails.rules.custom_category` / :func:`~cendor.guardrails.rules.denied_topics` /
:func:`~cendor.guardrails.rules.groundedness` / :func:`~cendor.guardrails.rules.intent`.

The similarity checks take a **bring-your-own** ``embed(text) -> sequence[float]`` — cendor ships no
model. :func:`local_embedder` closes the "but I have to wire an embedder first" gap with a local,
``$0`` default: **model2vec** static embeddings (numpy-only, **no torch**, ~8–30 MB), mirroring
``cassette``'s ``local_embedding_scorer`` precedent. The model is downloaded from Hugging Face at
the
user's choice on first use and **never bundled**; without the ``[embeddings]`` extra the call
raises a
clear, actionable error. Nothing here downloads anything unless you construct the embedder *and*
run a
check — so building an agent still makes no network call, and it stays opt-in (no rule reaches for
it
silently).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["local_embedder"]

#: The default model2vec checkpoint — small, fast, English-leaning static embeddings.
DEFAULT_MODEL = "minishlab/potion-base-8M"


def local_embedder(model: str = "minishlab/potion-base-8M") -> Callable[[str], Sequence[float]]:
    """A free, offline ``embed(text) -> list[float]`` backed by **model2vec** static embeddings.

    Needs the optional extra: ``pip install 'cendor-guardrails[embeddings]'`` (installs model2vec —
    numpy-only, no torch). ``model`` is any model2vec checkpoint (default
    ``"minishlab/potion-base-8M"``); it is loaded from Hugging Face on the **first** call and cached
    for the life of the returned function, so construction is cheap and a check only pays the load
    once. Hand the result to ``rules.custom_category(..., embed=…)`` / ``rules.denied_topics`` /
    ``rules.groundedness`` / ``rules.intent``.

    ```python
    from cendor.guardrails import rules, embeddings

    embed = embeddings.local_embedder()                     # offline, $0 after the one-time pull
    rail = rules.custom_category("code_requests",
        ["write a program", "build an app"], embed=embed, action="flag")
    ```

    Language coverage and quality are the model's, not a cendor claim — calibrate the threshold on
    your own inputs (the semantic checks are tuned heuristics, and there is no catch-rate claim).
    """
    state: dict[str, Any] = {"encoder": None}

    def _encoder() -> Any:
        if state["encoder"] is None:
            try:
                from model2vec import StaticModel  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise ImportError(
                    "local_embedder needs the 'embeddings' extra: "
                    "pip install 'cendor-guardrails[embeddings]' (installs model2vec — numpy-only, "
                    "no torch). Or pass your own embed(text) to the semantic rules."
                ) from exc
            state["encoder"] = StaticModel.from_pretrained(model)
        return state["encoder"]

    def embed(text: str) -> Sequence[float]:
        row = _encoder().encode([text])[0]
        return [float(x) for x in row]

    return embed
