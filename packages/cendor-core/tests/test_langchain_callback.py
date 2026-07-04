"""WS-D: CendorCallbackHandler records usage/reasoning/cost/tools/correlation from LangChain.

Drives the *real* LangChain / LangGraph stack with the OpenAI HTTP endpoint mocked by respx — no
network, no key. The callback path touches no client, so it sidesteps `with_raw_response` (usage
loss) and the streaming context-manager crash, and LangChain's own `usage_metadata` carries
reasoning. It is **recording-only**: the `instrument()` enforcement seam is not on this path.
"""

import pytest

respx = pytest.importorskip("respx")
httpx = pytest.importorskip("httpx")
pytest.importorskip("langchain_openai")

from cendor.core import bus  # noqa: E402
from cendor.core.instrument import add_interceptor, remove_interceptor  # noqa: E402
from cendor.core.langchain import CendorCallbackHandler  # noqa: E402
from cendor.core.types import LLMCall, ToolCall  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

CHAT = "https://api.openai.com/v1/chat/completions"


@pytest.fixture
def events():
    bus._reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()


def cj(content, usage, finish="stop", tool_calls=None):
    """A Chat Completions JSON response body."""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": usage,
    }


REASONING_USAGE = {
    "prompt_tokens": 200,
    "completion_tokens": 1200,
    "total_tokens": 1400,
    "completion_tokens_details": {"reasoning_tokens": 1000},
}

SSE = (
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"gpt-4o",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"gpt-4o",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"gpt-4o","choices":[],'
    '"usage":{"prompt_tokens":200,"completion_tokens":1200,"total_tokens":1400,'
    '"completion_tokens_details":{"reasoning_tokens":1000}}}\n\n'
    "data: [DONE]\n\n"
)


def test_invoke_captures_usage_reasoning_and_cost(events):
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-fake", callbacks=[CendorCallbackHandler()])
    with respx.mock:
        respx.post(CHAT).mock(return_value=httpx.Response(200, json=cj("Hi", REASONING_USAGE)))
        llm.invoke("hello")

    calls = [e for e in events if isinstance(e, LLMCall)]
    assert len(calls) == 1
    call = calls[0]
    assert call.usage.input_tokens == 200
    assert call.usage.output_tokens == 1200
    assert call.usage.reasoning_tokens == 1000  # reasoning captured via usage_metadata
    assert call.model.startswith("gpt-4o")
    assert call.cost is not None and call.cost.amount > 0  # priced offline
    assert call.metadata["source"] == "langchain"
    assert call.trace_id  # correlation id present (run_id, since no parent)


def test_stream_captures_usage_via_callback(events):
    llm = ChatOpenAI(
        model="gpt-4o", api_key="sk-fake", stream_usage=True, callbacks=[CendorCallbackHandler()]
    )
    with respx.mock:
        respx.post(CHAT).mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=SSE.encode()
            )
        )
        out = "".join(chunk.content for chunk in llm.stream("hi"))

    assert "Hi" in out
    calls = [e for e in events if isinstance(e, LLMCall)]
    assert len(calls) == 1
    assert calls[0].usage.output_tokens == 1200
    assert calls[0].usage.reasoning_tokens == 1000  # reasoning survives the streaming callback path


def test_per_call_config_callbacks(events):
    # The handler can be attached per call via config= instead of on the model.
    llm = ChatOpenAI(model="gpt-4o", api_key="sk-fake")
    with respx.mock:
        respx.post(CHAT).mock(
            return_value=httpx.Response(
                200, json=cj("ok", {"prompt_tokens": 5, "completion_tokens": 2})
            )
        )
        llm.invoke("hi", config={"callbacks": [CendorCallbackHandler()]})
    assert len([e for e in events if isinstance(e, LLMCall)]) == 1


def test_langgraph_agent_correlates_calls_and_emits_toolcall(events):
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    @tool
    def weather(city: str) -> str:
        """Return the weather in a city."""
        return f"Sunny in {city}"

    llm = ChatOpenAI(model="gpt-4o", api_key="sk-fake")
    agent = create_react_agent(llm, [weather])
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "weather", "arguments": '{"city": "Paris"}'},
        }
    ]
    with respx.mock:
        respx.post(CHAT).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=cj(
                        None,
                        {"prompt_tokens": 50, "completion_tokens": 15},
                        finish="tool_calls",
                        tool_calls=tool_calls,
                    ),
                ),
                httpx.Response(
                    200, json=cj("Paris is sunny.", {"prompt_tokens": 80, "completion_tokens": 10})
                ),
            ]
        )
        agent.invoke(
            {"messages": [("user", "weather in Paris?")]},
            config={"callbacks": [CendorCallbackHandler()]},
        )

    llm_calls = [e for e in events if isinstance(e, LLMCall)]
    tool_events = [e for e in events if isinstance(e, ToolCall)]
    assert len(llm_calls) == 2  # the react loop: decide-tool, then answer
    assert all(c.trace_id for c in llm_calls)
    assert len({c.trace_id for c in llm_calls}) == 1  # both nodes share one parent run -> one id
    assert len(tool_events) == 1
    tc = tool_events[0]
    assert tc.name == "weather"
    assert "Sunny in Paris" in str(tc.result)
    assert tc.trace_id == llm_calls[0].trace_id  # tool + model calls share the one run's trace_id


def test_separate_agents_get_distinct_trace_ids(events):
    from langgraph.prebuilt import create_react_agent

    llm = ChatOpenAI(model="gpt-4o", api_key="sk-fake")
    agent_a = create_react_agent(llm, [])
    agent_b = create_react_agent(llm, [])
    handler = CendorCallbackHandler()
    with respx.mock:
        respx.post(CHAT).mock(
            side_effect=[
                httpx.Response(
                    200, json=cj("A done", {"prompt_tokens": 10, "completion_tokens": 3})
                ),
                httpx.Response(
                    200, json=cj("B done", {"prompt_tokens": 12, "completion_tokens": 4})
                ),
            ]
        )
        agent_a.invoke({"messages": [("user", "a")]}, config={"callbacks": [handler]})
        agent_b.invoke({"messages": [("user", "b")]}, config={"callbacks": [handler]})

    trace_ids = [e.trace_id for e in events if isinstance(e, LLMCall)]
    assert len(trace_ids) == 2
    assert trace_ids[0] != trace_ids[1]  # distinct agent runs -> distinct correlation ids


def test_enforcement_seam_is_not_invoked_on_callback_path(events):
    # Enforcement lives on the instrument() interceptor seam. The callback path never touches a
    # client, so a blocking interceptor is never consulted — recording-only, by design.
    hit = []

    def blocker(event):
        hit.append(event)
        raise AssertionError("the enforcement seam must not run on the LangChain callback path")

    add_interceptor(blocker)
    try:
        llm = ChatOpenAI(model="gpt-4o", api_key="sk-fake", callbacks=[CendorCallbackHandler()])
        with respx.mock:
            respx.post(CHAT).mock(
                return_value=httpx.Response(
                    200, json=cj("unenforced", {"prompt_tokens": 5, "completion_tokens": 2})
                )
            )
            out = llm.invoke("hi")  # completes normally — nothing blocks it
    finally:
        remove_interceptor(blocker)

    assert hit == []  # interceptor never ran
    assert "unenforced" in out.content  # the app got its answer
    assert any(isinstance(e, LLMCall) for e in events)  # …but the call WAS recorded


def test_handler_never_raises_into_the_app(events):
    # A malformed LLMResult must not crash on_llm_end (recording is best-effort).
    handler = CendorCallbackHandler()
    handler.on_llm_end(object(), run_id="r1")  # no generations/llm_output — must be swallowed
    handler.on_tool_end("result", run_id="r2")  # no matching on_tool_start — must be swallowed
    # No exception raised; a ToolCall with default name is still emitted for the orphan tool end.
    assert any(isinstance(e, ToolCall) for e in events)
