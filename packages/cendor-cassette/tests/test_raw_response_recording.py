"""Recording a **raw-response** call — the shape MAF and langchain-openai drive OpenAI through.

Red-first. Measured against the real openai SDK on 2026-07-27
(`plan/evidence-ripple-followup-2026-07-27/probe_b3_cassette_envelope.py`): recording a
``client.responses.with_raw_response.create(...)`` or
``client.chat.completions.with_raw_response.create(...)`` call raised

    RecursionError: maximum recursion depth exceeded

**out of the caller's own ``create()``** — the app broke, not just the recording — and left a valid
but empty cassette on disk. Cause: ``_to_jsonable`` walked ``vars()`` of the envelope, which owns
the whole httpx response/client object graph.

Two things are asserted here:

1. ``_to_jsonable`` is **total** — no object graph, however deep or self-referential, can make it
   raise. A recorder that can crash the app it is recording is not a test tool.
2. A raw-response call records its **payload** (published by ``cendor-core`` ≥ 1.14.2 as
   ``metadata["response_body"]``) and replays as an envelope-shaped value, so the caller's
   ``raw.parse()`` keeps working offline.

Mock clients only, no network.
"""

from types import SimpleNamespace

import pytest
from cendor import cassette
from cendor.cassette import _to_jsonable
from cendor.core import bus, instrument

CHAT_BODY = {
    "id": "chatcmpl-1",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "recorded"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12},
}


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


class Envelope:
    """openai's raw-response envelope: headers + a buffered body — and, crucially, a reference
    graph that loops back on itself (the response holds the client, the client holds the response),
    which is what sent ``_to_jsonable`` to the recursion limit."""

    def __init__(self, body: dict, calls: dict) -> None:
        self.headers = {"x-request-id": "req_1"}
        self.status_code = 200
        self._body = body
        self.http_response = SimpleNamespace(json=lambda: body)
        self._client = SimpleNamespace()
        self._client.last_response = self  # the cycle
        calls["n"] += 1

    def parse(self) -> object:
        return SimpleNamespace(**self._body)


def raw_client(calls: dict):
    """A client whose `with_raw_response.create` is the instrumented `create` — the same shape
    openai builds, where the raw accessor wraps the already-wrapped method."""

    class Completions:
        def create(self, **kwargs):
            return Envelope(CHAT_BODY, calls)

    comp = Completions()
    client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    client.chat.completions.with_raw_response = SimpleNamespace(create=comp.create)
    return client


# --- 1. the serializer must be total ----------------------------------------------------------


def test_to_jsonable_survives_a_self_referential_object():
    node = SimpleNamespace(name="a")
    node.self = node  # a cycle
    out = _to_jsonable(node)
    assert isinstance(out, dict) and out["name"] == "a"


def test_to_jsonable_survives_an_unbounded_chain():
    head = SimpleNamespace(depth=0)
    node = head
    for i in range(1, 5000):
        node.child = SimpleNamespace(depth=i)
        node = node.child
    out = _to_jsonable(head)  # must not raise RecursionError
    assert isinstance(out, dict) and out["depth"] == 0


def test_to_jsonable_still_serializes_ordinary_nesting():
    obj = SimpleNamespace(a=1, b=[SimpleNamespace(c="x")], d={"e": SimpleNamespace(f=True)})
    assert _to_jsonable(obj) == {"a": 1, "b": [{"c": "x"}], "d": {"e": {"f": True}}}


# --- 2. recording + replaying a raw-response call ---------------------------------------------


def test_recording_a_raw_response_call_does_not_crash_the_caller(tmp_path):
    calls = {"n": 0}
    path = tmp_path / "raw.json"
    with cassette.using(str(path), mode="record"):
        client = raw_client(calls)
        client.chat.completions.with_raw_response.create(model="gpt-4o-mini", messages=[])

    assert calls["n"] == 1
    text = path.read_text(encoding="utf-8")
    assert '"recorded"' in text, "the payload must be recorded, not the envelope's object graph"


def test_a_recorded_raw_response_replays_as_an_envelope(tmp_path):
    calls = {"n": 0}
    path = tmp_path / "raw.json"
    with cassette.using(str(path), mode="record"):
        client = raw_client(calls)
        client.chat.completions.with_raw_response.create(model="gpt-4o-mini", messages=[])
    assert calls["n"] == 1

    with cassette.using(str(path), mode="replay"):
        client = raw_client(calls)
        out = client.chat.completions.with_raw_response.create(model="gpt-4o-mini", messages=[])

    assert calls["n"] == 1, "replay must not reach the client"
    assert callable(getattr(out, "parse", None)), "the caller's next move is raw.parse()"
    assert out.parse().choices[0].message.content == "recorded"
    assert hasattr(out, "headers")


def test_an_ordinary_call_is_recorded_and_replayed_unchanged(tmp_path):
    calls = {"n": 0}
    path = tmp_path / "plain.json"

    def make():
        class Completions:
            def create(self, **kwargs):
                calls["n"] += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="plain"))],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                )

        return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))

    with cassette.using(str(path), mode="record"):
        make().chat.completions.create(model="gpt-4o-mini", messages=[])
    with cassette.using(str(path), mode="replay"):
        out = make().chat.completions.create(model="gpt-4o-mini", messages=[])

    assert calls["n"] == 1
    assert out.choices[0].message.content == "plain"
    assert not hasattr(out, "parse"), "a normal response must not grow an envelope shape"
