"""The abstraction: Verdict / Context / Guardrail / the decorator / decision + exception types."""

from __future__ import annotations

from datetime import datetime

import pytest
from cendor.guardrails import (
    ALLOW,
    STAGES,
    Context,
    Guardrail,
    GuardrailDecision,
    GuardrailTripped,
    Verdict,
    guardrail,
    normalize_stages,
)


def test_verdict_rejects_unknown_action():
    with pytest.raises(ValueError, match="unknown action"):
        Verdict("nuke")


def test_guardrail_metadata_merged_onto_decision(decisions):
    from cendor.guardrails import apply

    g = Guardrail(
        name="tagged",
        stages=("input",),
        check=lambda p, c: Verdict("flag", reason="hit"),
        metadata={"severity": "high", "owner": "sec"},
    )
    apply([g], "input", "x", Context(stage="input", metadata={"owner": "override", "req": "r1"}))
    d = decisions[-1]
    assert d.metadata["severity"] == "high"  # from the guardrail's static metadata
    assert d.metadata["req"] == "r1"  # from the per-call context
    assert d.metadata["owner"] == "override"  # ctx.metadata wins a key clash


def test_decorator_accepts_metadata():
    @guardrail(stage="input", metadata={"team": "trust"})
    def check(payload, ctx):
        return None

    assert check.metadata == {"team": "trust"}


def test_verdict_defaults():
    v = Verdict("flag")
    assert v.reason == "" and v.replacement is None


def test_allow_is_none():
    assert ALLOW is None


def test_normalize_stages_from_str():
    assert normalize_stages("input") == ("input",)


def test_normalize_stages_from_tuple():
    assert normalize_stages(("input", "output")) == ("input", "output")


def test_normalize_stages_rejects_unknown():
    with pytest.raises(ValueError, match="unknown stage"):
        normalize_stages("nope")


def test_normalize_stages_rejects_empty():
    with pytest.raises(ValueError, match="at least one stage"):
        normalize_stages(())


def test_stages_constant_is_the_four():
    assert STAGES == ("input", "tool_call", "tool_output", "output")


def test_guardrail_normalizes_stages_in_post_init():
    g = Guardrail(name="x", stages="input", check=lambda p, c: None)  # type: ignore[arg-type]
    assert g.stages == ("input",)


def test_decorator_bare_defaults_to_input():
    @guardrail
    def my_check(payload, ctx):
        return None

    assert isinstance(my_check, Guardrail)
    assert my_check.name == "my_check" and my_check.stages == ("input",)


def test_decorator_with_stage_and_name():
    @guardrail(stage="output", name="renamed")
    def my_check(payload, ctx):
        return None

    assert my_check.name == "renamed" and my_check.stages == ("output",)


def test_decorator_multi_stage():
    @guardrail(stage=("input", "output"))
    def my_check(payload, ctx):
        return None

    assert my_check.stages == ("input", "output")


def test_context_defaults():
    ctx = Context(stage="input")
    assert ctx.agent == "" and ctx.tool == "" and ctx.trace_id == "" and ctx.metadata == {}


def test_guardrail_decision_defaults_ts_and_metadata():
    d = GuardrailDecision(guardrail="g", stage="input", action="block")
    assert isinstance(d.ts, datetime) and d.metadata == {}


def test_guardrail_tripped_carries_decisions_and_message():
    d = GuardrailDecision(guardrail="g", stage="input", action="block", reason="nope")
    exc = GuardrailTripped([d])
    assert exc.decisions == [d]
    assert "g" in str(exc) and "nope" in str(exc)
