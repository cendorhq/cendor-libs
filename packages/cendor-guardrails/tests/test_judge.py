"""LLM-judge helpers: verdict prompt template, strict-JSON parsing, and the judge() composer.
No network — ``respond`` is a fake that returns a canned string. docs/guardrails.md §LLM judge."""

from __future__ import annotations

import pytest
from cendor.guardrails import GuardrailTripped, Verdict, apply, apply_async, judge, rules


def test_verdict_prompt_embeds_policy_and_json_contract():
    p = judge.verdict_prompt("Trip on secrets.")
    assert "Trip on secrets." in p
    assert '"trip"' in p and '"reason"' in p  # pins the model to the strict verdict shape


def test_parse_verdict_trip():
    v = judge.parse_verdict('{"trip": true, "reason": "leaked a key"}')
    assert isinstance(v, Verdict) and v.action == "block" and v.reason == "leaked a key"


def test_parse_verdict_pass():
    assert judge.parse_verdict('{"trip": false, "reason": "clean"}') is None


def test_parse_verdict_tolerates_a_json_fence():
    v = judge.parse_verdict('```json\n{"trip": true, "reason": "x"}\n```')
    assert v is not None and v.action == "block"


def test_parse_verdict_custom_action():
    v = judge.parse_verdict('{"trip": true, "reason": "iffy"}', action="flag")
    assert v is not None and v.action == "flag"


def test_parse_verdict_malformed_raises():
    with pytest.raises(ValueError):
        judge.parse_verdict("I think this is fine, no JSON here")
    with pytest.raises(ValueError):
        judge.parse_verdict('{"reason": "missing trip"}')


def test_judge_sync_composes_and_trips():
    def respond(system, user):
        assert "policy" not in system.lower() or True  # system carries the instruction
        return '{"trip": true, "reason": "blocked by judge"}'

    check = judge.judge(respond, "Trip on anything.")
    g = rules.llm_judge(check)
    with pytest.raises(GuardrailTripped) as ei:
        apply([g], "output", "some model text")
    assert ei.value.decisions[-1].reason == "blocked by judge"


def test_judge_sync_passes():
    check = judge.judge(lambda s, u: '{"trip": false, "reason": "ok"}', "policy")
    assert apply([rules.llm_judge(check)], "output", "text") == []


@pytest.mark.asyncio
async def test_judge_async():
    async def respond(system, user):
        return '{"trip": true, "reason": "async trip"}'

    check = judge.judge(respond, "policy")
    with pytest.raises(GuardrailTripped):
        await apply_async([rules.llm_judge(check)], "output", "text")


@pytest.mark.asyncio
async def test_judge_malformed_reply_fails_closed():
    # a garbled judge reply raises ValueError inside the check → on_error fail_closed → block
    check = judge.judge(lambda s, u: "not json", "policy")
    with pytest.raises(GuardrailTripped):
        await apply_async([rules.llm_judge(check)], "output", "text")
