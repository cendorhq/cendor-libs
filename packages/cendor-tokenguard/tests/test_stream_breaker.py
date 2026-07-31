"""TG-STREAM-BREAKER — on_exceed="break": mid-stream cut when the running estimate crosses the cap.

Red-first (GC-D10): headline 5×N cut, out-of-scope drain, USD allowance, exactly-one-raise, thinking
counting, replay-cut. Offline (replay streams) — deterministic, no network.
"""

from types import SimpleNamespace

import cendor.tokenguard as tg
import pytest
from cendor.core import bus, instrument
from cendor.tokenguard import BudgetEvent, BudgetExceeded, budget


@pytest.fixture
def events():
    bus._reset()
    tg.reset()
    seen: list = []
    bus.subscribe(seen.append)
    yield seen
    bus._reset()
    tg.reset()


def _content(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


class _ClosableStream:
    """A sync stream that records close() — so the abort path can be verified."""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self.closed = True


def _client(stream):
    class Completions:
        def create(self, **kwargs):
            return stream

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _budget_events(events):
    return [e for e in events if isinstance(e, BudgetEvent)]


def _llm_calls(events):
    from cendor.core.types import LLMCall

    return [e for e in events if isinstance(e, LLMCall)]


# --- headline: 5×N cut -----------------------------------------------------------------------


def test_headline_break_cuts_runaway_stream(events):
    # A stream that would emit ~5×N tokens under a tokens=N break budget: consumed is bounded,
    # BudgetExceeded comes from the loop, the underlying stream is closed, exactly one LLMCall emits
    # flagged estimated, and one BudgetEvent(action="broken") fires.
    stream = _ClosableStream([_content("one two three four five ") for _ in range(50)])
    client = instrument(_client(stream))

    got = []
    with budget(tokens=20, on_exceed="break", name="stream-cap"):
        s = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        with pytest.raises(BudgetExceeded, match="mid-stream break"):
            for ch in s:
                got.append(ch)

    assert 0 < len(got) < 50  # cut well before the 50-chunk stream drained
    assert stream.closed is True  # underlying provider stream closed on the cut
    calls = _llm_calls(events)
    assert len(calls) == 1  # exactly one settle/emit
    assert calls[0].metadata.get("usage_estimated") is True  # partial estimate
    assert calls[0].metadata.get("streamed") is True
    broken = [e for e in _budget_events(events) if e.action == "broken"]
    assert len(broken) == 1
    assert broken[0].name == "stream-cap"
    assert broken[0].cap_tokens == 20


def test_break_exactly_one_raise(events):
    # The settle must NOT raise a second BudgetExceeded after the mid-stream cut.
    stream = _ClosableStream([_content("alpha beta gamma delta ") for _ in range(30)])
    client = instrument(_client(stream))
    raises = 0
    with budget(tokens=15, on_exceed="break"):
        s = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        try:
            for _ in s:
                pass
        except BudgetExceeded:
            raises += 1
    assert raises == 1  # one, not two
    assert len(_llm_calls(events)) == 1


# --- out-of-scope drain ----------------------------------------------------------------------


def test_break_fires_when_stream_drained_outside_scope(events):
    # Frames are captured at initiation (GLR-5), so the breaker still cuts a stream drained after
    # the
    # with-block exits.
    stream = _ClosableStream([_content("word word word ") for _ in range(40)])
    client = instrument(_client(stream))
    with budget(tokens=12, on_exceed="break"):
        s = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
    # scope exited — now drain
    got = []
    with pytest.raises(BudgetExceeded):
        for ch in s:
            got.append(ch)
    assert 0 < len(got) < 40
    assert stream.closed is True


# --- USD allowance ---------------------------------------------------------------------------


def test_break_usd_cap_cuts_stream(events):
    # A USD break budget converts headroom to an integer token allowance once, then cuts.
    stream = _ClosableStream([_content("some words here to bill ") for _ in range(200)])
    client = instrument(_client(stream))
    got = []
    with budget(usd=0.001, on_exceed="break"):  # gpt-4o output $1e-5/tok -> ~100 tok allowance
        s = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
        with pytest.raises(BudgetExceeded):
            for ch in s:
                got.append(ch)
    assert 0 < len(got) < 200
    broken = [e for e in _budget_events(events) if e.action == "broken"]
    assert len(broken) == 1


def test_break_second_stream_sees_reduced_allowance(events):
    # Sequential streams under one break budget: the second sees spend from the first (settle
    # updates
    # frame.spent), so its allowance shrinks — the cumulative gate the breaker enforces per-stream.
    with budget(tokens=60, on_exceed="break") as b:
        s1 = _ClosableStream([_content("aa bb cc ") for _ in range(4)])  # small, stays under
        c1 = instrument(_client(s1))
        list(c1.chat.completions.create(model="gpt-4o", messages=[], stream=True))
        spent_after_first = b.spent  # noqa: F841 (documents intent)
        # A big second stream must be cut, since spent from the first eats into the 60-token cap.
        s2 = _ClosableStream([_content("dd ee ff gg hh ") for _ in range(80)])
        c2 = instrument(_client(s2))
        got = []
        with pytest.raises(BudgetExceeded):
            for ch in c2.chat.completions.create(model="gpt-4o", messages=[], stream=True):
                got.append(ch)
    assert 0 < len(got) < 80


# --- thinking counting -----------------------------------------------------------------------


def test_break_counts_visible_thinking(events):
    # Anthropic thinking_delta text counts toward the running estimate, so a heavy-thinking stream
    # is
    # cut even before much visible answer text.
    thinking_chunks = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="reasoning step number here "),
        )
        for _ in range(60)
    ]

    class Messages:
        def create(self, **kwargs):
            return _ClosableStream(thinking_chunks)

    client = instrument(SimpleNamespace(messages=Messages()))
    got = []
    with budget(tokens=25, on_exceed="break"):
        s = client.messages.create(model="claude-opus-4-8", messages=[], stream=True)
        with pytest.raises(BudgetExceeded):
            for ch in s:
                got.append(ch)
    assert 0 < len(got) < 60  # cut on thinking text alone
    call = _llm_calls(events)[0]
    assert call.usage.reasoning_tokens > 0


# --- replay-cut nuance -----------------------------------------------------------------------


def test_break_cuts_replayed_stream_and_settles_partial(events):
    # A replayed stream (cassette-style: an interceptor short-circuits with recorded chunks)
    # observes
    # too and cuts; the settle reflects the PARTIAL (consumed through the cut), like a live cut.
    from cendor.core import MISS, add_interceptor, remove_interceptor
    from cendor.core.types import LLMCall

    recorded = [_content("recorded token stream ") for _ in range(50)]

    def replayer(call):
        if isinstance(call, LLMCall):
            return recorded
        return MISS

    add_interceptor(replayer)
    try:
        client = instrument(_client(_ClosableStream([])))  # underlying never consumed (replayed)
        got = []
        with budget(tokens=18, on_exceed="break"):
            s = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)
            with pytest.raises(BudgetExceeded):
                for ch in s:
                    got.append(ch)
        assert 0 < len(got) < 50  # replay cut mid-way, not the full recording
        call = _llm_calls(events)[0]
        assert call.metadata.get("usage_estimated") is True
        from cendor.core import tokens as core_tokens

        full = core_tokens.count("recorded token stream " * 50, "gpt-4o")
        # settle counts the partial (consumed) chunks, not the full 50-chunk recording
        assert 0 < call.usage.output_tokens < full
    finally:
        remove_interceptor(replayer)


def test_break_validates_and_type_surface():
    # break is a valid on_exceed value; a typo still raises ValueError eagerly.
    budget(tokens=10, on_exceed="break")  # no raise
    with pytest.raises(ValueError, match="on_exceed"):
        budget(tokens=10, on_exceed="brake")  # typo


# --- Gemini streaming (google-genai `generate_content_stream`, core >= 1.15) -------------------
#
# The breaker rides core's stream-observer seam, so it works for any capture path core adds — but
# "should" is not "does": before core 1.15 a Gemini stream emitted no LLMCall at all, so a
# `budget(..., on_exceed="break")` around one was silently inert. Pinned here, on the real chunk
# shape (`.text` + a cumulative `usage_metadata`), with a real cadence.


class _ClosableGeminiStream:
    """A google-genai-shaped stream: chunks carry `.text`, and `close()` is observable."""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self.closed = True


def _gemini_chunk(text, prompt=4, candidates=0):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt, candidates_token_count=candidates, thoughts_token_count=None
        ),
    )


def _gemini_client(stream):
    class Models:
        def generate_content_stream(self, **kwargs):
            return stream

    return SimpleNamespace(models=Models())


def test_break_cuts_a_gemini_stream_mid_flight(events):
    chunks = [_gemini_chunk("one two three four five ", candidates=5 * (i + 1)) for i in range(50)]
    stream = _ClosableGeminiStream(chunks)
    client = instrument(_gemini_client(stream))

    got = []
    with budget(tokens=20, on_exceed="break", name="gemini-stream-cap"):
        s = client.models.generate_content_stream(model="gemini-2.5-flash", contents="go")
        with pytest.raises(BudgetExceeded, match="mid-stream break"):
            for ch in s:
                got.append(ch)

    assert 0 < len(got) < 50, "cut well before the 50-chunk stream drained"
    assert stream.closed is True, "the underlying provider stream was closed"
    calls = _llm_calls(events)
    assert len(calls) == 1 and calls[0].provider == "google"
    assert calls[0].metadata.get("streamed") is True
    broken = [e for e in _budget_events(events) if e.action == "broken"]
    assert len(broken) == 1 and broken[0].name == "gemini-stream-cap"


def test_gemini_stream_under_the_cap_completes_and_settles(events):
    # Negative control: the same wiring must NOT cut a stream that stays inside the budget.
    stream = _ClosableGeminiStream(
        [_gemini_chunk("hi ", candidates=2), _gemini_chunk("there", candidates=4)]
    )
    client = instrument(_gemini_client(stream))
    with budget(tokens=10_000, on_exceed="break"):
        got = list(client.models.generate_content_stream(model="gemini-2.5-flash", contents="go"))
    assert len(got) == 2
    assert stream.closed is False
    calls = _llm_calls(events)
    assert len(calls) == 1
    assert calls[0].usage.output_tokens == 4  # real cumulative usage, not an estimate
    assert not calls[0].metadata.get("usage_estimated")
    assert not [e for e in _budget_events(events) if e.action == "broken"]
