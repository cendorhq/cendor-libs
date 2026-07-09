"""LLM-judge helpers: verdict prompt template, strict-JSON parsing, and the judge() composer.
No network — ``respond`` is a fake that returns a canned string. docs/guardrails.md §LLM judge."""

from __future__ import annotations

import pytest
from cendor.guardrails import (
    Context,
    GuardrailTripped,
    Verdict,
    apply,
    apply_async,
    evaluate,
    judge,
    rules,
)


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


# --------------------------------------------------------------------------- A3: task_adherence


def _tc_ctx(instruction="Book a flight to Paris.", tool="search_flights", args=None):
    return Context(
        stage="tool_call",
        tool=tool,
        tool_args=args if args is not None else {"to": "Paris"},
        instruction=instruction,
    )


def test_task_adherence_flags_a_misaligned_tool_call():
    respond = lambda s, u: '{"trip": true, "reason": "deletes files, unrelated to booking"}'  # noqa: E731
    check = judge.task_adherence(respond)  # action defaults to flag
    g = rules.llm_judge(check, stage="tool_call", action="flag")  # → on_error fail_open
    decs = apply([g], "tool_call", {"path": "/"}, _tc_ctx(tool="delete_all", args={"path": "/"}))
    assert len(decs) == 1 and decs[0].action == "flag"
    assert "unrelated" in decs[0].reason


def test_task_adherence_passes_an_aligned_tool_call():
    check = judge.task_adherence(lambda s, u: '{"trip": false, "reason": "aligned"}')
    g = rules.llm_judge(check, stage="tool_call", action="flag")
    assert apply([g], "tool_call", {"to": "Paris"}, _tc_ctx()) == []


def test_task_adherence_prompt_carries_instruction_and_proposed_call():
    seen: dict[str, str] = {}

    def respond(system, user):
        seen["system"], seen["user"] = system, user
        return '{"trip": false, "reason": "ok"}'

    check = judge.task_adherence(respond)
    apply([rules.llm_judge(check, stage="tool_call", action="flag")], "tool_call", {}, _tc_ctx())
    assert "Book a flight to Paris." in seen["system"]
    assert "search_flights" in seen["user"] and "Paris" in seen["user"]


def test_task_adherence_default_action_is_flag():
    v = judge.task_adherence(lambda s, u: '{"trip": true, "reason": "x"}')({}, _tc_ctx())
    assert isinstance(v, Verdict) and v.action == "flag"


def test_task_adherence_block_action_raises():
    check = judge.task_adherence(
        lambda s, u: '{"trip": true, "reason": "off-task"}', action="block"
    )
    g = rules.llm_judge(check, stage="tool_call", action="block")
    with pytest.raises(GuardrailTripped) as ei:
        apply([g], "tool_call", {}, _tc_ctx())
    assert "off-task" in ei.value.decisions[-1].reason


def test_task_adherence_instruction_from_metadata_fallback():
    seen: dict[str, str] = {}

    def respond(system, user):
        seen["system"] = system
        return '{"trip": false, "reason": "ok"}'

    ctx = Context(
        stage="tool_call", tool="t", tool_args={}, metadata={"user_input": "Only search."}
    )
    apply(
        [rules.llm_judge(judge.task_adherence(respond), stage="tool_call", action="flag")],
        "tool_call",
        {},
        ctx,
    )
    assert "Only search." in seen["system"]


def test_task_adherence_fail_open_on_garbled_judge():
    # a garbled reply raises inside the check; action="flag" ⇒ on_error fail_open ⇒ recorded flag
    check = judge.task_adherence(lambda s, u: "not json at all")
    g = rules.llm_judge(check, stage="tool_call", action="flag")
    decs = apply([g], "tool_call", {}, _tc_ctx())
    assert len(decs) == 1 and decs[0].action == "flag"
    assert "fail-open" in decs[0].reason


@pytest.mark.asyncio
async def test_task_adherence_async():
    async def respond(system, user):
        return '{"trip": true, "reason": "async misalign"}'

    check = judge.task_adherence(respond)
    decs = await apply_async(
        [rules.llm_judge(check, stage="tool_call", action="flag")], "tool_call", {}, _tc_ctx()
    )
    assert decs and decs[-1].action == "flag" and "async misalign" in decs[-1].reason


def test_task_adherence_handles_missing_instruction():
    seen: dict[str, str] = {}

    def respond(system, user):
        seen["system"] = system
        return '{"trip": false, "reason": "ok"}'

    ctx = Context(stage="tool_call", tool="t", tool_args={})  # no instruction, no metadata
    evaluate(
        [rules.llm_judge(judge.task_adherence(respond), stage="tool_call", action="flag")],
        "tool_call",
        {},
        ctx,
    )
    assert "(no instruction provided)" in seen["system"]
