"""G2 — custom_category (semantic category-by-example) + the local_embedder ignition. A fake embed
exercises the cosine routing; the model2vec extra is optional (skipped when absent). No network."""

from __future__ import annotations

import pytest
from cendor.guardrails import Context, GuardrailTripped, apply, embeddings, rules

_VECS = {
    "write a program": [1.0, 0.0, 0.0],
    "build an app": [0.95, 0.1, 0.0],
    "create a hello world app": [0.98, 0.05, 0.0],
    "what is the capital of france": [0.0, 0.0, 1.0],
}


def _embed(text: str):
    return _VECS.get(text.strip(), [0.0, 0.0, 0.0])


def _v(rule, payload, stage="input"):
    return rule.check(payload, Context(stage=stage))


def test_custom_category_trips_on_paraphrase():
    g = rules.custom_category(
        "code_requests", ["write a program", "build an app"], embed=_embed, threshold=0.8
    )
    v = _v(g, "create a hello world app")  # a paraphrase keyword_deny would miss
    assert v is not None
    assert v.metadata["category"] == "code_requests" and v.metadata["score"] >= 0.8


def test_custom_category_passes_when_unrelated():
    g = rules.custom_category("code_requests", ["write a program"], embed=_embed, threshold=0.8)
    assert _v(g, "what is the capital of france") is None


def test_custom_category_defaults_to_flag():
    g = rules.custom_category("x", ["write a program"], embed=_embed)
    assert _v(g, "create a hello world app").action == "flag"


def test_custom_category_can_block():
    g = rules.custom_category("x", ["write a program"], embed=_embed, action="block")
    with pytest.raises(GuardrailTripped):
        apply([g], "input", "create a hello world app")


def test_custom_category_empty_examples_never_trips():
    g = rules.custom_category("x", [], embed=_embed)
    assert _v(g, "write a program") is None


def test_custom_category_embeds_lazily():
    calls: list[str] = []

    def counting(text: str):
        calls.append(text)
        return _embed(text)

    g = rules.custom_category("x", ["write a program"], embed=counting)
    assert calls == []  # construction embeds nothing
    _v(g, "build an app")
    assert "write a program" in calls  # exemplar embedded on first check


def test_custom_category_reexported_on_rules():
    assert rules.custom_category is not None


# --------------------------------------------------------------------------- local_embedder (extra)


def test_local_embedder_requires_the_extra_or_embeds():
    pytest.importorskip("model2vec")  # only runs when the [embeddings] extra is installed
    embed = embeddings.local_embedder()
    vec = embed("hello world")
    assert isinstance(vec, list) and len(vec) > 0 and all(isinstance(x, float) for x in vec)


def test_local_embedder_construction_makes_no_call():
    # constructing the embedder must not load a model (lazy on first embed)
    embed = embeddings.local_embedder()
    assert callable(embed)
