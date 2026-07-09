"""A2 — annotation-parity evidence: the reserved GuardrailDecision.metadata keys (severity /
detected / filtered / redacted / citation / license). No shape change — a check attaches them via
Verdict.metadata and the engine merges them into the decision's metadata (under Context.metadata,
which still wins). Adapters populate them from a vendor result. No network — fake clients.
docs/specs/bus-events.md "Reserved annotation keys"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.guardrails import (
    Context,
    GuardrailTripped,
    Verdict,
    apply,
    evaluate,
    guardrail,
    rules,
)

# --------------------------------------------------------------------------- Verdict.metadata merge


def test_verdict_metadata_lands_on_the_decision(decisions):
    @guardrail(stage="input")
    def sev(payload, ctx):
        return Verdict("flag", reason="risky", metadata={"severity": "high", "detected": True})

    apply([sev], "input", "text")
    assert decisions[-1].metadata == {"severity": "high", "detected": True}


def test_context_metadata_wins_over_verdict_metadata(decisions):
    @guardrail(stage="input")
    def sev(payload, ctx):
        return Verdict("flag", reason="x", metadata={"severity": "low", "detected": True})

    ctx = Context(stage="input", metadata={"severity": "high"})
    apply([sev], "input", "text", ctx)
    # verdict severity="low" is layered under the caller's ctx severity="high"; detected still rides
    assert decisions[-1].metadata == {"severity": "high", "detected": True}


def test_verdict_metadata_defaults_empty(decisions):
    @guardrail(stage="input")
    def plain(payload, ctx):
        return Verdict("flag", reason="x")

    apply([plain], "input", "text")
    assert decisions[-1].metadata == {}


def test_static_guardrail_metadata_still_stamped(decisions):
    # the W3 policy_hash path (static Guardrail.metadata) composes with a verdict annotation
    @guardrail(stage="input")
    def sev(payload, ctx):
        return Verdict("flag", reason="x", metadata={"detected": True})

    from cendor.guardrails import Guardrail

    g = Guardrail(
        name="sev", stages=("input",), check=sev.check, metadata={"policy_hash": "sha256:abc"}
    )
    apply([g], "input", "text")
    assert decisions[-1].metadata == {"policy_hash": "sha256:abc", "detected": True}


# --------------------------------------------------------------------------- adapters populate keys


def _moderation_client(flagged: bool, categories: dict):
    result = SimpleNamespace(flagged=flagged, categories=SimpleNamespace(**categories))
    return SimpleNamespace(
        moderations=SimpleNamespace(create=lambda **kw: SimpleNamespace(results=[result]))
    )


def test_openai_moderation_sets_detected_and_filtered(decisions):
    client = _moderation_client(True, {"violence": True, "hate": False})
    with pytest.raises(GuardrailTripped):
        apply([rules.openai_moderation(client, action="block")], "input", "text")
    md = decisions[-1].metadata
    assert md.get("detected") is True and md.get("filtered") is True


def test_openai_moderation_flag_action_is_not_filtered(decisions):
    client = _moderation_client(True, {"violence": True})
    apply([rules.openai_moderation(client, action="flag")], "input", "text")
    md = decisions[-1].metadata
    assert md.get("detected") is True and md.get("filtered") is False


def test_bedrock_intervened_sets_detected_filtered(decisions):
    resp = {"action": "GUARDRAIL_INTERVENED", "actionReason": "blocked topic"}
    client = SimpleNamespace(apply_guardrail=lambda **kw: resp)
    with pytest.raises(GuardrailTripped):
        apply([rules.bedrock_guardrail(client, "gr-1", action="block")], "input", "text")
    md = decisions[-1].metadata
    assert md.get("detected") is True and md.get("filtered") is True


def test_bedrock_redact_sets_redacted(decisions):
    resp = {
        "action": "GUARDRAIL_INTERVENED",
        "actionReason": "pii",
        "outputs": [{"text": "masked ****"}],
    }
    client = SimpleNamespace(apply_guardrail=lambda **kw: resp)
    cleaned, _ = evaluate(
        [rules.bedrock_guardrail(client, "gr-1", action="redact", stage="output")], "output", "raw"
    )
    assert cleaned == "masked ****"
    assert decisions[-1].metadata.get("redacted") is True


def test_azure_content_safety_sets_detected(decisions):
    resp = {"userPromptAnalysis": {"attackDetected": True}}
    client = SimpleNamespace(shield_prompt=lambda **kw: resp)
    with pytest.raises(GuardrailTripped):
        apply([rules.azure_content_safety(client, action="block")], "input", "attack")
    assert decisions[-1].metadata.get("detected") is True


def test_model_armor_sets_detected(decisions):
    resp = {
        "sanitizationResult": {
            "filterMatchState": "MATCH_FOUND",
            "filterResults": {"pi_and_jailbreak": {"matchState": "MATCH_FOUND"}},
        }
    }
    client = SimpleNamespace(
        sanitize_user_prompt=lambda **kw: resp,
        sanitize_model_response=lambda **kw: resp,
    )
    with pytest.raises(GuardrailTripped):
        apply([rules.model_armor(client, "projects/p/locations/l/templates/t")], "input", "x")
    assert decisions[-1].metadata.get("detected") is True
