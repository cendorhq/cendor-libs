"""instrument_tool emits ToolCall; interceptors short-circuit calls (replay). No network."""

from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument, instrument_tool
from cendor.core.instrument import MISS, Reroute, add_interceptor, remove_interceptor
from cendor.core.types import LLMCall, ToolCall


def test_interceptor_symbols_are_exported_from_cendor_core():
    # M7: add_interceptor / remove_interceptor / MISS are documented top-level in core.md and
    # exported top-level by @cendor/core — they must import from `cendor.core` too (parity), not
    # only from the private `cendor.core.instrument`.
    from cendor.core import MISS as MISS_public
    from cendor.core import add_interceptor as add_public
    from cendor.core import remove_interceptor as remove_public
    from cendor.core.instrument import MISS as MISS_private
    from cendor.core.instrument import add_interceptor as add_private

    assert add_public is add_private
    assert MISS_public is MISS_private
    assert callable(add_public) and callable(remove_public)


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def test_instrument_tool_emits_toolcall(events):
    @instrument_tool
    def search(query, top_k=3):
        return [f"result for {query}"]

    out = search("refunds", top_k=2)
    assert out == ["result for refunds"]
    assert len(events) == 1
    tc = events[0]
    assert isinstance(tc, ToolCall)
    assert tc.name == "search"
    assert tc.arguments == {"args": ["refunds"], "kwargs": {"top_k": 2}}
    assert tc.result == ["result for refunds"]


def test_instrument_tool_named_and_idempotent(events):
    @instrument_tool("lookup")
    def f(x):
        return x

    wrapped_again = instrument_tool("lookup")(f)
    assert wrapped_again is f  # idempotent
    f(5)
    assert events[0].name == "lookup"


async def test_async_tool(events):
    @instrument_tool
    async def fetch(url):
        return "body"

    assert await fetch("http://x") == "body"
    assert events[0].name == "fetch"


def test_interceptor_replays_llm_call_without_running_it(events):
    calls = {"n": 0}

    class Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    canned = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20))

    def replayer(event):
        return canned if isinstance(event, LLMCall) else MISS

    add_interceptor(replayer)
    try:
        resp = client.chat.completions.create(model="gpt-4o", messages=[])
    finally:
        remove_interceptor(replayer)

    assert resp is canned
    assert calls["n"] == 0  # real method never ran
    assert events[0].metadata.get("replayed") is True
    assert events[0].usage.input_tokens == 10  # usage taken from the replayed response


def test_interceptor_can_reroute_the_request(events):
    seen_models = []

    class Completions:
        def create(self, **kwargs):
            seen_models.append(kwargs["model"])
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    def router(event):
        return Reroute(model="gpt-4o-mini") if isinstance(event, LLMCall) else MISS

    add_interceptor(router)
    try:
        client.chat.completions.create(model="gpt-4o", messages=[])
    finally:
        remove_interceptor(router)

    assert seen_models == ["gpt-4o-mini"]  # real call ran with the rerouted model
    assert events[0].model == "gpt-4o-mini"  # event reflects the model actually used
    assert events[0].metadata.get("rerouted") is True


def test_interceptor_can_reroute_the_messages(events):
    # Reroute(messages=…) rewrites the outbound messages the provider receives (used by acttrace's
    # guard() for redact-before-send) and keeps the emitted event consistent with what was sent.
    seen = {}

    class Completions:
        def create(self, **kwargs):
            seen["messages"] = kwargs["messages"]
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    scrubbed = [{"role": "user", "content": "mail me at <redacted>"}]

    def rewriter(event):
        return Reroute(messages=scrubbed) if isinstance(event, LLMCall) else MISS

    add_interceptor(rewriter)
    try:
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "mail me at a@b.com"}]
        )
    finally:
        remove_interceptor(rewriter)

    assert seen["messages"] == scrubbed  # provider received the rewritten messages
    assert events[0].messages == scrubbed  # emitted event reflects what was actually sent
    assert events[0].metadata.get("rerouted") is True


def test_reroute_messages_maps_to_the_responses_api_input_kwarg(events):
    # The OpenAI Responses API carries messages on `input`, not `messages` — Reroute(messages=…)
    # must rewrite the right provider kwarg.
    seen = {}

    class Responses:
        def create(self, **kwargs):
            seen["kwargs"] = kwargs
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    client = instrument(SimpleNamespace(responses=Responses()))
    scrubbed = [{"role": "user", "content": "<redacted>"}]

    def rewriter(event):
        return Reroute(messages=scrubbed) if isinstance(event, LLMCall) else MISS

    add_interceptor(rewriter)
    try:
        client.responses.create(model="gpt-4o", input="secret")
    finally:
        remove_interceptor(rewriter)

    assert seen["kwargs"].get("input") == scrubbed  # rewritten onto `input`
    assert "messages" not in seen["kwargs"]  # no spurious `messages` kwarg leaked


async def test_interceptor_can_reroute_messages_async(events):
    seen = {}

    class Completions:
        async def create(self, **kwargs):
            seen["messages"] = kwargs["messages"]
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    scrubbed = [{"role": "user", "content": "<redacted>"}]

    def rewriter(event):
        return Reroute(messages=scrubbed) if isinstance(event, LLMCall) else MISS

    add_interceptor(rewriter)
    try:
        await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "a@b.com"}]
        )
    finally:
        remove_interceptor(rewriter)

    assert seen["messages"] == scrubbed
    assert events[0].messages == scrubbed


def test_interceptor_can_reroute_messages_streaming(events):
    seen = {}

    class Completions:
        def create(self, **kwargs):
            seen["messages"] = kwargs["messages"]

            def chunks():
                yield SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), choices=[]
                )

            return chunks()

    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    scrubbed = [{"role": "user", "content": "<redacted>"}]

    def rewriter(event):
        return Reroute(messages=scrubbed) if isinstance(event, LLMCall) else MISS

    add_interceptor(rewriter)
    try:
        stream = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "a@b.com"}], stream=True
        )
        list(stream)  # drain so the LLMCall is emitted
    finally:
        remove_interceptor(rewriter)

    assert seen["messages"] == scrubbed  # rewritten before the streaming call ran
    assert events[0].messages == scrubbed
