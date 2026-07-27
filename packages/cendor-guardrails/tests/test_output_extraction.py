"""The standalone output stage reads assistant text off a completed LLMCall across provider shapes.

Exercises the best-effort ``_response_text`` extractor directly (private, but the multi-provider
surface is worth pinning) plus the graceful skip when nothing is extractable.
"""

from __future__ import annotations

from types import SimpleNamespace

from cendor.core.types import LLMCall
from cendor.guardrails import _response_text


def _call(response) -> LLMCall:
    call = LLMCall(id="1", provider="x", model="m", messages=[])
    call.metadata["response"] = response
    return call


def test_openai_chat_completions_shape():
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi there"))])
    assert _response_text(_call(resp)) == "hi there"


def test_openai_responses_output_text_shape():
    assert _response_text(_call(SimpleNamespace(output_text="responded"))) == "responded"


def test_anthropic_content_blocks_shape():
    resp = SimpleNamespace(content=[SimpleNamespace(text="part one "), SimpleNamespace(text="two")])
    assert _response_text(_call(resp)) == "part one two"


def test_ollama_message_shape():
    resp = {"message": {"content": "ollama says hi"}}
    assert _response_text(_call(resp)) == "ollama says hi"


def test_gemini_text_shape():
    assert _response_text(_call(SimpleNamespace(text="gemini text"))) == "gemini text"


def test_bedrock_converse_shape():
    resp = {"output": {"message": {"content": [{"text": "bedrock "}, {"text": "reply"}]}}}
    assert _response_text(_call(resp)) == "bedrock reply"


def test_no_response_metadata_returns_none():
    assert _response_text(LLMCall(id="1", provider="x", model="m", messages=[])) is None


def test_unrecognized_shape_returns_none():
    assert _response_text(_call(SimpleNamespace(mystery=1))) is None


# --- a raw-response envelope (MAF / langchain-openai) ------------------------------------------


class _Envelope:
    """openai's raw-response envelope: headers + an un-parsed body, and no assistant text of its
    own. `client.responses.with_raw_response.create(...)` returns one, so the output stage saw
    nothing and silently skipped — the banned text was delivered."""

    def __init__(self, body: dict) -> None:
        self.headers = {"x-request-id": "req_1"}
        self.http_response = SimpleNamespace(json=lambda: body)


BODY = {"choices": [{"message": {"role": "assistant", "content": "banned text"}}]}


def test_a_raw_response_envelope_alone_yields_nothing():
    # the envelope carries no assistant text; without the decoded body there is nothing to gate on
    assert _response_text(_call(_Envelope(BODY))) is None


def test_the_decoded_body_published_by_core_is_used():
    # cendor-core >= 1.14.2 publishes metadata["response_body"] for exactly this reason
    call = _call(_Envelope(BODY))
    call.metadata["response_body"] = BODY
    assert _response_text(call) == "banned text"


def test_the_envelope_wins_nothing_over_a_normal_response():
    # a normal call has no response_body, so the existing path is untouched
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="fine"))])
    assert _response_text(_call(resp)) == "fine"
