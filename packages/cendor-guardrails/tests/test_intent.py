"""G3 — pre-LLM intent screening: embedding + classifier backends, deny/allow modes, judge helper.
A fake embed/classify (no model, no network) exercises the routing. docs/guardrails.md."""

from __future__ import annotations

import pytest
from cendor.guardrails import Context, GuardrailTripped, apply, judge, rules

# Deterministic fake embedding: known phrases → fixed vectors so cosine is predictable.
_VECS = {
    "write a program": [1.0, 0.0, 0.0],
    "book a flight": [0.0, 1.0, 0.0],
    "create a hello world app": [0.98, 0.05, 0.0],  # ~ close to "write a program"
    "what is the weather": [0.0, 0.0, 1.0],  # orthogonal to both
}


def _embed(text: str):
    return _VECS.get(text.strip(), [0.0, 0.0, 0.0])


def _v(rule, payload, stage="input"):
    return rule.check(payload, Context(stage=stage))


# --------------------------------------------------------------------------- embedding backend


def test_intent_deny_trips_on_semantic_match():
    g = rules.intent({"code": ["write a program"]}, embed=_embed, mode="deny", threshold=0.8)
    v = _v(g, "create a hello world app")  # ~0.98 to the exemplar
    assert v is not None and v.metadata["intent"] == "code" and v.metadata["score"] >= 0.8


def test_intent_deny_passes_when_far():
    g = rules.intent({"code": ["write a program"]}, embed=_embed, mode="deny", threshold=0.8)
    assert _v(g, "what is the weather") is None  # orthogonal


def test_intent_allow_trips_when_off_topic():
    g = rules.intent(
        {"code": ["write a program"], "travel": ["book a flight"]},
        embed=_embed,
        mode="allow",
        threshold=0.8,
    )
    assert _v(g, "what is the weather") is not None  # matches neither → off-topic
    assert _v(g, "create a hello world app") is None  # matches "code" → allowed


def test_intent_defaults_to_flag_action():
    g = rules.intent({"code": ["write a program"]}, embed=_embed)
    v = _v(g, "create a hello world app")
    assert v.action == "flag"


def test_intent_can_block():
    g = rules.intent({"code": ["write a program"]}, embed=_embed, mode="deny", action="block")
    with pytest.raises(GuardrailTripped):
        apply([g], "input", "create a hello world app")


# --------------------------------------------------------------------------- classifier backend


def test_intent_classifier_label_string():
    g = rules.intent(["spam"], classify=lambda t: "spam", mode="deny")
    v = _v(g, "buy now")
    assert v is not None and v.metadata["intent"] == "spam"


def test_intent_classifier_mapping_argmax():
    def clf(text):
        return {"spam": 0.9, "ham": 0.1}

    g = rules.intent(["spam"], classify=clf, mode="deny", threshold=0.8)
    assert _v(g, "x") is not None


def test_intent_classifier_allow_off_topic():
    g = rules.intent(["support"], classify=lambda t: "sales", mode="allow")
    assert _v(g, "x") is not None  # detected "sales" not in allowed {"support"}


# --------------------------------------------------------------------------- guards / errors


def test_intent_requires_exactly_one_backend():
    with pytest.raises(ValueError, match="exactly one"):
        rules.intent({"a": ["x"]})  # neither embed nor classify
    with pytest.raises(ValueError, match="exactly one"):
        rules.intent({"a": ["x"]}, embed=_embed, classify=lambda t: "a")


def test_intent_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown mode"):
        rules.intent({"a": ["x"]}, embed=_embed, mode="maybe")


# --------------------------------------------------------------------------- judge helper


def test_intent_prompt_deny_and_allow_wording():
    deny = judge.intent_prompt({"medical": ..., "legal": ...}, mode="deny")
    assert "refuse" in deny and "medical" in deny and "legal" in deny
    allow = judge.intent_prompt(["support", "billing"], mode="allow")
    assert "only help" in allow and "support" in allow


def test_intent_prompt_composes_with_judge_judge():
    # the LLM backend: intent_prompt → judge.judge → rules.llm_judge
    policy = judge.intent_prompt(["support"], mode="allow")
    replies = iter(['{"trip": true, "reason": "off-topic"}'])
    check = judge.judge(lambda system, user: next(replies), policy, action="flag")
    rail = rules.llm_judge(check, stage="input", action="flag")
    v = _v(rail, "tell me a joke")
    assert v is not None and v.action == "flag"
