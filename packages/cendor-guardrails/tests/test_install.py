"""Standalone wiring: install() registers one core interceptor + an output subscriber.

Block raises pre-spend, redact reroutes to the provider, pass declines (MISS), tool_call blocks,
and the output stage raises post-flight — all with a fake instrumented client (no network).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cendor.core import instrument
from cendor.core.instrument import instrument_tool
from cendor.guardrails import GuardrailTripped, Verdict, install, rules, uninstall


def msgs(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def make_client(calls: dict, response=None):
    class Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            calls["last_kwargs"] = kwargs
            return response or SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


@pytest.fixture(autouse=True)
def _uninstall_after():
    yield
    uninstall()


def test_input_block_raises_before_the_call_spends():
    calls = {"n": 0}
    client = make_client(calls)
    install([rules.keyword_deny(["forbidden"], action="block")])
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=msgs("a forbidden thing"))
    assert calls["n"] == 0  # blocked pre-spend — the provider was never called


def test_input_redact_reroutes_cleaned_messages_to_provider():
    calls = {"n": 0}
    client = make_client(calls)
    install([rules.regex_rule(r"sk-\w+", action="redact", stage="input")])
    client.chat.completions.create(model="gpt-4o", messages=msgs("my key sk-abc123"))
    assert calls["n"] == 1
    assert calls["last_kwargs"]["messages"][0]["content"] == "my key [redacted]"


def test_pass_declines_and_the_call_proceeds_normally():
    calls = {"n": 0}
    client = make_client(calls)
    install([rules.keyword_deny(["forbidden"], action="block")])
    client.chat.completions.create(model="gpt-4o", messages=msgs("perfectly fine"))
    assert calls["n"] == 1
    assert calls["last_kwargs"]["messages"][0]["content"] == "perfectly fine"  # untouched


def test_tool_call_block_raises():
    install([rules.keyword_deny(["rm -rf"], stage="tool_call", action="block")])

    @instrument_tool("shell")
    def shell(cmd):
        return "ran"

    with pytest.raises(GuardrailTripped):
        shell("rm -rf /")


def test_tool_call_flag_records_and_proceeds(decisions):
    install([rules.keyword_deny(["danger"], stage="tool_call", action="flag")])

    @instrument_tool("shell")
    def shell(cmd):
        return "ran"

    assert shell("danger cmd") == "ran"  # tools have no rewrite seam — flag records, call proceeds
    assert decisions[0].stage == "tool_call" and decisions[0].tool == "shell"


def test_output_subscriber_blocks_post_flight():
    calls = {"n": 0}
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="the secret plan"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    client = make_client(calls, response=resp)
    install([rules.keyword_deny(["secret"], stage="output", action="block")])
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=msgs("hi"))
    assert calls["n"] == 1  # post-flight: the call already ran (documented overshoot)


def _stream_chunks(*words: str) -> list:
    # Chat Completions streamed delta chunks + a final usage-only chunk (empty choices).
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=w))]) for w in words
    ]
    chunks.append(
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    )
    return chunks


def _stream_client(chunks: list):
    class Completions:
        def create(self, **kwargs):
            def gen():
                yield from chunks

            return gen()

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_output_subscriber_blocks_streamed_response():
    # M2: core stores a streamed response's metadata["response"] as a LIST of delta chunks. The
    # output stage must reconstruct the completed text from the chunks and still block — before the
    # fix it read None off the list and silently no-oped, delivering the banned text.
    client = _stream_client(_stream_chunks("the ", "secret ", "plan"))
    install([rules.keyword_deny(["secret"], stage="output", action="block")])
    with pytest.raises(GuardrailTripped):
        stream = client.chat.completions.create(model="gpt-4o", messages=msgs("hi"), stream=True)
        for _ in stream:  # consuming finalizes the stream -> emits -> output stage reconstructs
            pass


def test_output_subscriber_passes_clean_streamed_response():
    # The mirror: a streamed response with no banned text must NOT raise (no false positive).
    client = _stream_client(_stream_chunks("a ", "perfectly ", "fine ", "answer"))
    install([rules.keyword_deny(["secret"], stage="output", action="block")])
    stream = client.chat.completions.create(model="gpt-4o", messages=msgs("hi"), stream=True)
    for _ in stream:
        pass  # no raise expected


def test_uninstall_removes_interceptor_and_subscriber():
    calls = {"n": 0}
    client = make_client(calls)
    install([rules.keyword_deny(["forbidden"], action="block")])
    uninstall()
    client.chat.completions.create(model="gpt-4o", messages=msgs("a forbidden thing"))
    assert calls["n"] == 1  # after uninstall the guardrail no longer fires


def test_install_replaces_a_prior_install():
    calls = {"n": 0}
    client = make_client(calls)
    install([rules.keyword_deny(["first"], action="block")])
    install([rules.keyword_deny(["second"], action="block")])  # replaces the first
    client.chat.completions.create(model="gpt-4o", messages=msgs("mentions first only"))
    assert calls["n"] == 1  # the first guardrail is gone
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=msgs("mentions second"))


def test_install_async_check_raises_on_the_sync_seam():
    async def acheck(payload, ctx):
        return Verdict("block")

    calls = {"n": 0}
    client = make_client(calls)
    install([rules.custom(acheck, stage="input", name="a")])
    with pytest.raises(TypeError, match="async"):
        client.chat.completions.create(model="gpt-4o", messages=msgs("x"))
