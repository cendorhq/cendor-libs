"""Execution-model maturity: per-guardrail timeout + on_error policy, and the scoped() context
manager. No network — a "slow" check is a ``time.sleep`` / ``asyncio.sleep``, a "failing" check
raises. docs/guardrails.md §Execution model."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from cendor.core import instrument
from cendor.guardrails import (
    GuardrailTripped,
    Verdict,
    apply,
    apply_async,
    guardrail,
    rules,
    scoped,
)

# --------------------------------------------------------------------------- on_error


def _boom(payload, ctx):  # a check that always raises
    raise RuntimeError("judge unreachable")


def test_on_error_fail_closed_blocks(decisions):
    g = rules.custom(_boom, on_error="fail_closed")
    with pytest.raises(GuardrailTripped):
        apply([g], "input", "hi")
    # the failure is recorded as evidence, not swallowed
    assert decisions[-1].action == "block"
    assert "errored" in decisions[-1].reason


def test_on_error_fail_open_flags_and_continues(decisions):
    g = rules.custom(_boom, on_error="fail_open")
    out = apply([g], "input", "hi")  # does NOT raise
    assert out[-1].action == "flag"
    assert "fail-open" in out[-1].reason
    assert decisions[-1].action == "flag"


def test_on_error_default_from_action():
    # a flag-action llm_judge defaults to fail_open; a block-action one to fail_closed
    assert rules.llm_judge(_boom, action="flag").on_error == "fail_open"
    assert rules.llm_judge(_boom, action="block").on_error == "fail_closed"
    # custom defaults to fail_closed (safe)
    assert rules.custom(_boom).on_error == "fail_closed"


def test_reason_never_leaks_payload(decisions):
    def raise_with_secret(payload, ctx):
        raise RuntimeError("noise")

    apply([rules.custom(raise_with_secret, on_error="fail_open")], "input", "sk-VERYSECRET")
    assert "sk-VERYSECRET" not in decisions[-1].reason


# --------------------------------------------------------------------------- timeout (sync)


def test_sync_timeout_trips_on_error():
    def slow(payload, ctx):
        time.sleep(0.5)
        return None

    g = rules.custom(slow, timeout=0.05, on_error="fail_closed")
    start = time.perf_counter()
    with pytest.raises(GuardrailTripped):
        apply([g], "input", "x")
    assert time.perf_counter() - start < 0.4  # returned well before the check would have finished


def test_sync_timeout_fail_open_passes():
    def slow(payload, ctx):
        time.sleep(0.5)
        return Verdict("block")

    out = apply([rules.custom(slow, timeout=0.05, on_error="fail_open")], "input", "x")
    assert out and out[-1].action == "flag"


def test_no_timeout_runs_to_completion():
    def quick(payload, ctx):
        return Verdict("flag", reason="ok")

    out = apply([rules.custom(quick)], "input", "x")
    assert out[-1].action == "flag" and out[-1].reason == "ok"


# --------------------------------------------------------------------------- timeout (async)


@pytest.mark.asyncio
async def test_async_timeout_trips_on_error():
    async def slow(payload, ctx):
        await asyncio.sleep(0.5)
        return None

    g = rules.custom(slow, timeout=0.05, on_error="fail_closed")
    with pytest.raises(GuardrailTripped):
        await apply_async([g], "input", "x")


@pytest.mark.asyncio
async def test_async_timeout_fail_open_flags():
    async def slow(payload, ctx):
        await asyncio.sleep(0.5)
        return Verdict("block")

    out = await apply_async([rules.custom(slow, timeout=0.05, on_error="fail_open")], "input", "x")
    assert out[-1].action == "flag"


# --------------------------------------------------------------------------- Guardrail validation


def test_guardrail_validates_on_error_and_timeout():
    with pytest.raises(ValueError):
        guardrail(on_error="nonsense")(lambda p, c: None)
    with pytest.raises(ValueError):
        guardrail(timeout=0)(lambda p, c: None)


# --------------------------------------------------------------------------- scoped()


def _client():
    calls: list = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    return client, calls


def test_scoped_gates_only_inside_the_block():
    client, calls = _client()
    gr = rules.keyword_deny(["forbidden"], action="block")
    # inside the scope: blocked pre-spend
    with pytest.raises(GuardrailTripped):
        with scoped([gr]):
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "forbidden"}]
            )
    assert calls == []  # $0 — the model was never called
    # outside the scope: the same call goes through
    client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "forbidden"}]
    )
    assert len(calls) == 1


def test_scoped_redacts_before_send():
    client, calls = _client()
    gr = rules.regex_rule(r"\bsk-[A-Za-z0-9]{16,}\b", action="redact", stage="input")
    with scoped([gr]):
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "key sk-ABCD1234EFGH5678IJ"}]
        )
    assert "[redacted]" in calls[-1]["messages"][0]["content"]
    assert "sk-ABCD" not in calls[-1]["messages"][0]["content"]


def test_scoped_nesting_restores_outer():
    client, calls = _client()
    inner = rules.keyword_deny(["inner"], action="block")
    with scoped([rules.keyword_deny(["outer"], action="block")]):
        with scoped([inner]):
            # only "inner" trips here; "outer" does not
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "outer"}]
            )
        # back to the outer scope: "outer" trips again
        with pytest.raises(GuardrailTripped):
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "outer"}]
            )
    assert len(calls) == 1  # the "inner"-scope call (with "outer" text) went through


@pytest.mark.asyncio
async def test_scoped_isolates_concurrent_tasks():
    # two concurrent tasks with different scopes must not see each other's guardrails
    client, calls = _client()

    async def task(word: str) -> bool:
        with scoped([rules.keyword_deny([word], action="block")]):
            await asyncio.sleep(0)  # yield so the tasks interleave
            try:
                client.chat.completions.create(
                    model="gpt-4o", messages=[{"role": "user", "content": "alpha"}]
                )
                return False
            except GuardrailTripped:
                return True

    a_blocked, b_blocked = await asyncio.gather(task("alpha"), task("beta"))
    assert a_blocked is True  # task A's scope denies "alpha"
    assert b_blocked is False  # task B's scope denies "beta" only — "alpha" passes
