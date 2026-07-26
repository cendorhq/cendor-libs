"""Usage survives a **raw-response envelope**, and ``responses.parse`` is captured at all.

Red-first. Microsoft Agent Framework 1.12.1 drives OpenAI through
``client.responses.with_raw_response.create(stream=False, …)`` (and ``.parse(…)`` when a
``text_format`` is set) — see ``agent_framework_openai/_chat_client.py``. Two consequences, both
measured against the real SDK in ``plan/evidence-cendor-libs-ripple-2026-07-26/``:

* **F-3** — the value handed back is an *envelope* (openai's ``LegacyAPIResponse``: headers + the
  un-parsed body) with no ``usage`` of its own, so a governed MAF turn reported ``usage=None`` and
  ``cost=None`` while the identical ``responses.create`` call priced exactly. Anything that wants
  response headers hits this, not just MAF.
* **N-3** — ``responses.parse`` was not an instrumented entrypoint, so MAF's structured-output
  branch emitted **no event at all**.

No network — duck-typed mock clients only (the envelope shape is asserted against the real SDK in
the evidence pack, not here).
"""

import asyncio
from types import SimpleNamespace

import pytest
from cendor.core import bus, instrument
from cendor.core.types import Usage

RESPONSES_BODY = {
    "id": "resp_1",
    "model": "gpt-4o-mini",
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    "usage": {
        "input_tokens": 14,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 2,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 16,
    },
}

CHAT_BODY = {
    "id": "chatcmpl-1",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12},
}


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


class Envelope:
    """openai's raw-response envelope, duck-typed: headers + a buffered body, **no** ``usage``."""

    def __init__(self, body: dict, readable: bool = True) -> None:
        self.headers = {"x-request-id": "req_1"}
        self.status_code = 200
        self._body = body
        self._readable = readable
        self.http_response = SimpleNamespace(json=self._json)

    def _json(self) -> dict:
        if not self._readable:
            # what a with_streaming_response envelope does: the body was never read
            raise RuntimeError("ResponseNotRead: the response body has not been read")
        return self._body

    def parse(self) -> object:  # the caller's way back to a model; core must not need it
        return SimpleNamespace(**self._body)


def responses_client(returns: object):
    async def create(**kwargs):
        return returns

    return instrument(SimpleNamespace(responses=SimpleNamespace(create=create)))


# --- F-3: read usage off the envelope --------------------------------------------------------


def test_raw_response_envelope_yields_usage_and_cost(events):
    client = responses_client(Envelope(RESPONSES_BODY))
    asyncio.run(client.responses.create(model="gpt-4o-mini", input="say ok"))

    assert len(events) == 1
    call = events[0]
    assert call.provider == "openai" and call.model == "gpt-4o-mini"
    assert call.usage == Usage(input_tokens=14, output_tokens=2)
    assert call.cost is not None and call.cost.amount > 0
    assert call.metadata["raw_response_envelope"] is True


def test_chat_completions_envelope_also_yields_usage(events):
    # the same envelope wraps a Chat Completions body (prompt_tokens/completion_tokens shape)
    async def create(**kwargs):
        return Envelope(CHAT_BODY)

    client = instrument(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    asyncio.run(client.chat.completions.create(model="gpt-4o-mini", messages=[]))

    assert events[0].usage == Usage(input_tokens=11, output_tokens=1)
    assert events[0].cost is not None


def test_an_unread_streaming_envelope_does_not_raise_and_stays_none(events):
    # with_streaming_response: the body is unread, so reading it raises. Capture must degrade to the
    # previous behaviour (usage None), never break the caller's call.
    client = responses_client(Envelope(RESPONSES_BODY, readable=False))
    out = asyncio.run(client.responses.create(model="gpt-4o-mini", input="say ok"))

    assert isinstance(out, Envelope)  # the caller still gets exactly what the SDK returned
    assert len(events) == 1
    assert events[0].usage is None
    assert events[0].cost is None
    assert "raw_response_envelope" not in events[0].metadata


def test_a_normal_response_is_unchanged(events):
    plain = SimpleNamespace(usage=SimpleNamespace(input_tokens=14, output_tokens=2))
    client = responses_client(plain)
    asyncio.run(client.responses.create(model="gpt-4o-mini", input="say ok"))

    assert events[0].usage == Usage(input_tokens=14, output_tokens=2)
    assert "raw_response_envelope" not in events[0].metadata  # the fallback never ran


def test_a_gateway_reported_cost_on_the_envelope_body_wins(events):
    body = {**RESPONSES_BODY, "usage": {**RESPONSES_BODY["usage"], "cost": "0.25"}}
    client = responses_client(Envelope(body))
    asyncio.run(client.responses.create(model="gpt-4o-mini", input="say ok"))

    assert str(events[0].cost.amount) == "0.25"
    assert events[0].metadata["cost_reported"] is True


# --- N-3: responses.parse is an entrypoint ---------------------------------------------------


def test_responses_parse_is_instrumented(events):
    async def parse(**kwargs):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=14, output_tokens=2))

    async def create(**kwargs):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    client = instrument(SimpleNamespace(responses=SimpleNamespace(create=create, parse=parse)))
    asyncio.run(client.responses.parse(model="gpt-4o-mini", input="say ok"))

    assert len(events) == 1, "a structured-output Responses call must emit exactly one LLMCall"
    assert events[0].provider == "openai"
    assert events[0].model == "gpt-4o-mini"
    assert events[0].usage == Usage(input_tokens=14, output_tokens=2)
    assert events[0].messages == [{"role": "user", "content": "say ok"}]


def test_responses_parse_through_a_raw_response_envelope(events):
    # MAF's actual structured-output line: with_raw_response.parse(...) ⇒ N-3 + F-3 together
    async def parse(**kwargs):
        return Envelope(RESPONSES_BODY)

    client = instrument(SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    asyncio.run(client.responses.parse(model="gpt-4o-mini", input="say ok"))

    assert events[0].usage == Usage(input_tokens=14, output_tokens=2)
    assert events[0].cost is not None


def test_a_client_without_parse_is_untouched(events):
    # an older openai SDK has no responses.parse — detection must not require it
    client = responses_client(
        SimpleNamespace(usage=SimpleNamespace(input_tokens=3, output_tokens=1))
    )
    assert not hasattr(client.responses, "parse")
    asyncio.run(client.responses.create(model="gpt-4o-mini", input="hi"))
    assert events[0].usage == Usage(input_tokens=3, output_tokens=1)
