"""GLR-7 — record/replay key off a session id stamped **at call initiation** (via the core ambient
seam), so a streamed call created in one session but drained while a *different* session's scope is
active (the concurrent-recording / detached-consumer case) is recorded into the correct cassette,
not lost or stolen by the wrong session."""

import json
from types import SimpleNamespace

from cendor import cassette
from cendor.core import bus, instrument
from cendor.core.ambient import _reset_ambient


def _delta(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


def _streaming_client():
    chunks = [
        _delta("Hel"),
        _delta("lo"),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)),
    ]

    class Completions:
        def create(self, **kwargs):
            return iter(chunks)

    return instrument(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))


def test_records_stream_drained_in_a_different_session(tmp_path):
    bus._reset()
    _reset_ambient()
    path_a = str(tmp_path / "a.json")
    path_b = str(tmp_path / "b.json")
    client = _streaming_client()
    out = ""
    with cassette.using(path_a, mode="record"):
        stream = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "a"}], stream=True
        )
        with cassette.using(path_b, mode="record"):
            for c in stream:  # A's stream drained inside session B
                if c.choices:
                    out += c.choices[0].delta.content
    assert out == "Hello"
    with open(path_a) as f:
        a = json.load(f)
    with open(path_b) as f:
        b = json.load(f)
    assert len(a["entries"]) == 1  # recorded in A (its creation session), not lost
    assert len(b["entries"]) == 0  # B does not steal A's event
