"""WS-B regression: LangChain streaming through an instrumented client no longer crashes.

`langchain_openai` consumes a streamed completion as a **context manager**
(``with client…create(stream=True) as response:``). Before WS-B the proxy was a bare generator, so
this raised ``TypeError: 'generator' object does not support the context manager protocol``. Here we
drive the *real* LangChain stack with the OpenAI HTTP endpoint mocked by respx — no network, no key.
"""

import pytest

respx = pytest.importorskip("respx")
httpx = pytest.importorskip("httpx")
pytest.importorskip("langchain_openai")

from cendor.core import bus, instrument  # noqa: E402
from cendor.core.types import LLMCall  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

CHAT = "https://api.openai.com/v1/chat/completions"

# A streamed completion terminated by a usage chunk that carries reasoning tokens (GPT-5 / o-series
# shape), so we can assert usage *and* reasoning survive the LangChain → proxy → bus path.
SSE = (
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"gpt-4o",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"gpt-4o",'
    '"choices":[{"index":0,"delta":{"content":" there"},"finish_reason":null}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"gpt-4o",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"gpt-4o","choices":[],'
    '"usage":{"prompt_tokens":200,"completion_tokens":1200,"total_tokens":1400,'
    '"completion_tokens_details":{"reasoning_tokens":1000}}}\n\n'
    "data: [DONE]\n\n"
)


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def test_langchain_stream_does_not_crash_and_captures_usage(events):
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-fake")
    instrument(llm.root_client)

    with respx.mock:
        respx.post(CHAT).mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=SSE.encode()
            )
        )
        out = "".join(chunk.content for chunk in llm.stream("hello"))

    assert "Hi there" in out  # the stream was consumed end-to-end, no TypeError
    calls = [e for e in events if isinstance(e, LLMCall)]
    assert len(calls) == 1  # exactly one LLMCall finalized for the streamed request
    call = calls[-1]
    assert call.metadata["streamed"] is True
    assert call.usage is not None
    assert call.usage.input_tokens == 200
    assert call.usage.output_tokens == 1200
    assert call.usage.reasoning_tokens == 1000  # reasoning survives the streaming path
    assert not call.metadata.get("usage_estimated")  # real provider usage, not an offline estimate
