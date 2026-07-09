"""Opt-in detection-tier adapters (Wave 2): classifier / prompt_guard / language / moderation.
No network, no ML deps — a fake classify/detect fn and a fake moderation client. docs/guardrails.md
"Threat model"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.guardrails import GuardrailTripped, adapters, apply, rules

# --------------------------------------------------------------------------- classifier


def test_classifier_float_score_trips_over_threshold():
    g = rules.classifier(lambda t: 0.8, threshold=0.5, action="block")
    with pytest.raises(GuardrailTripped) as ei:
        apply([g], "input", "text")
    assert "0.80 >= 0.5" in ei.value.decisions[-1].reason


def test_classifier_below_threshold_passes():
    assert apply([rules.classifier(lambda t: 0.3, threshold=0.5)], "input", "text") == []


def test_classifier_bool_and_mapping():
    assert (
        apply([rules.classifier(lambda t: True, action="flag")], "input", "x")[-1].action == "flag"
    )
    g = rules.classifier(
        lambda t: {"injection": 0.9, "benign": 0.1}, label="injection", threshold=0.5
    )
    with pytest.raises(GuardrailTripped):
        apply([g], "input", "x")


def test_classifier_reexported_on_both_surfaces():
    assert adapters.classifier is rules.classifier
    assert rules.prompt_guard is adapters.prompt_guard


# --------------------------------------------------------------------------- prompt_guard


def test_prompt_guard_missing_extra_raises_clear_error():
    # transformers is not installed in the base test env → a clear, actionable ImportError at check
    g = rules.prompt_guard(action="flag", on_error="fail_open")
    # on_error=fail_open turns the ImportError into a recorded flag rather than propagating
    out = apply([g], "input", "some prompt")
    assert out and out[-1].action == "flag"
    assert "promptguard" in out[-1].reason.lower() or "transformers" in out[-1].reason.lower()


def test_prompt_guard_fail_closed_blocks_on_missing_extra():
    # the default fail-closed policy: a missing backend blocks (never silently opens the gate)
    with pytest.raises(GuardrailTripped):
        apply([rules.prompt_guard()], "input", "some prompt")


# --------------------------------------------------------------------------- language


def test_language_trips_on_disallowed():
    g = rules.language(["en"], detect=lambda t: "fr", action="block")
    with pytest.raises(GuardrailTripped) as ei:
        apply([g], "input", "bonjour le monde")
    assert "'fr'" in ei.value.decisions[-1].reason


def test_language_allows_expected():
    assert apply([rules.language(["en", "fr"], detect=lambda t: "en")], "input", "hello") == []


def test_language_empty_payload_passes():
    assert apply([rules.language(["en"], detect=lambda t: "zz")], "input", "   ") == []


# --------------------------------------------------------------------------- openai_moderation


def _mod_client(flagged: bool, categories: dict):
    return SimpleNamespace(
        moderations=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                results=[SimpleNamespace(flagged=flagged, categories=categories)]
            )
        )
    )


def test_openai_moderation_blocks_when_flagged():
    client = _mod_client(True, {"violence": True, "hate": False})
    with pytest.raises(GuardrailTripped) as ei:
        apply([rules.openai_moderation(client, action="block")], "input", "bad")
    assert "violence" in ei.value.decisions[-1].reason


def test_openai_moderation_passes_when_clean():
    client = _mod_client(False, {"violence": False})
    assert apply([rules.openai_moderation(client)], "input", "hello") == []


def test_openai_moderation_category_filter():
    # flagged overall, but not in the categories we care about → pass
    client = _mod_client(True, {"self-harm": True, "violence": False})
    g = rules.openai_moderation(client, categories=["violence"], action="block")
    assert apply([g], "input", "text") == []
    # now the requested category is the one flagged → trip
    client2 = _mod_client(True, {"violence": True})
    with pytest.raises(GuardrailTripped):
        apply([rules.openai_moderation(client2, categories=["violence"])], "input", "text")
