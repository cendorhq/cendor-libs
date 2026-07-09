"""Similarity checks over a bring-your-own embedding fn: groundedness + denied_topics. A fake
`embed` (no model, no network) exercises the cosine math + thresholds. docs/guardrails.md."""

from __future__ import annotations

import pytest
from cendor.guardrails import GuardrailTripped, apply, rules

# A trivial deterministic "embedding": map known phrases to fixed orthonormal-ish vectors so cosine
# similarity is predictable without a real model.
_VECS = {
    "cats": [1.0, 0.0, 0.0],
    "cats are great pets": [0.99, 0.1, 0.0],
    "dogs": [0.0, 1.0, 0.0],
    "quantum chromodynamics": [0.0, 0.0, 1.0],
    "medical diagnosis": [0.0, 1.0, 0.0],
    "how do I treat a fever": [0.05, 0.99, 0.0],
}


def _embed(text: str):
    return _VECS.get(text.strip(), [0.0, 0.0, 0.0])


# --------------------------------------------------------------------------- groundedness


def test_groundedness_flags_ungrounded_answer():
    g = rules.groundedness(_embed, ["cats"], threshold=0.75, action="flag")
    out = apply([g], "output", "quantum chromodynamics")  # orthogonal to the source → sim 0
    assert out and out[-1].action == "flag"
    assert "ungrounded" in out[-1].reason


def test_groundedness_passes_when_grounded():
    g = rules.groundedness(_embed, ["cats"], threshold=0.75)
    assert apply([g], "output", "cats are great pets") == []  # sim ~0.99 >= 0.75


def test_groundedness_empty_sources_never_trips():
    assert apply([rules.groundedness(_embed, [])], "output", "anything") == []


def test_groundedness_can_block():
    g = rules.groundedness(_embed, ["cats"], threshold=0.9, action="block")
    with pytest.raises(GuardrailTripped):
        apply([g], "output", "dogs")


# --------------------------------------------------------------------------- denied_topics


def test_denied_topics_blocks_close_match():
    g = rules.denied_topics(_embed, ["medical diagnosis"], threshold=0.8, action="block")
    with pytest.raises(GuardrailTripped) as ei:
        apply([g], "input", "how do I treat a fever")  # sim ~0.99 to "medical diagnosis"
    assert "medical diagnosis" in ei.value.decisions[-1].reason


def test_denied_topics_passes_when_far():
    g = rules.denied_topics(_embed, ["medical diagnosis"], threshold=0.8)
    assert apply([g], "input", "cats") == []  # orthogonal


def test_denied_topics_reexported_on_rules():
    assert rules.groundedness is not None and rules.denied_topics is not None


def test_embed_only_called_for_sources_lazily():
    calls: list[str] = []

    def counting_embed(text: str):
        calls.append(text)
        return _embed(text)

    g = rules.groundedness(counting_embed, ["cats"])
    assert calls == []  # construction embeds nothing
    apply([g], "output", "dogs")
    assert "cats" in calls  # source embedded on first check
