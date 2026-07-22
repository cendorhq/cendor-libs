"""GLR-11a — the LangChain handler stamps an agent/chain/node name into ``metadata["agent"]``
(explicit ``metadata["agent"]`` > LangGraph ``langgraph_node`` > run name; unnamed plain chains stay
unnamed). Driven by calling the handler methods directly (no live LangGraph needed)."""

from types import SimpleNamespace

import pytest
from cendor.core import LLMCall, bus
from cendor.core.ambient import _reset_ambient

pytest.importorskip("langchain_core")
from cendor.core.langchain import CendorCallbackHandler  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    bus._reset()
    _reset_ambient()
    yield
    bus._reset()
    _reset_ambient()


def _chat_result(model="gpt-4o"):
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": 5, "output_tokens": 3},
        response_metadata={"model_name": model},
    )
    return SimpleNamespace(generations=[[SimpleNamespace(message=msg)]], llm_output={})


def _emitted_call():
    out: list = []
    bus.subscribe(lambda e: out.append(e) if isinstance(e, LLMCall) else None)
    return out


def test_stamps_langgraph_node_name():
    calls = _emitted_call()
    h = CendorCallbackHandler()
    h.on_chat_model_start({}, [], run_id="llm-1", metadata={"langgraph_node": "researcher"})
    h.on_llm_end(_chat_result(), run_id="llm-1")
    assert calls[0].metadata.get("agent") == "researcher"


def test_falls_back_to_run_name():
    calls = _emitted_call()
    h = CendorCallbackHandler()
    h.on_chat_model_start({}, [], run_id="solo", name="summarizer")
    h.on_llm_end(_chat_result(), run_id="solo")
    assert calls[0].metadata.get("agent") == "summarizer"


def test_explicit_metadata_agent_wins():
    calls = _emitted_call()
    h = CendorCallbackHandler()
    h.on_chat_model_start(
        {},
        [],
        run_id="solo",
        metadata={"agent": "explicit", "langgraph_node": "researcher"},
        name="summarizer",
    )
    h.on_llm_end(_chat_result(), run_id="solo")
    assert calls[0].metadata.get("agent") == "explicit"


def test_unnamed_plain_chain_stamps_no_agent():
    calls = _emitted_call()
    h = CendorCallbackHandler()
    h.on_chat_model_start({}, [], run_id="solo")
    h.on_llm_end(_chat_result(), run_id="solo")
    assert "agent" not in calls[0].metadata
