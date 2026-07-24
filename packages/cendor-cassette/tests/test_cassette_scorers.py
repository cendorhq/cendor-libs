"""M3a: fills the thin cassette test footprint — direct coverage for the scoring helpers
(`lexical_score`, `cosine`, `embedding_scorer`, `openai_embedding_scorer`,
`local_embedding_scorer`), `semantic_drift` edges, and `promote()` line-shape handling. Offline
and deterministic; mock embedders/clients only, no network.
"""

import json

import pytest
from cendor import cassette


# --------------------------------------------------------------------------- lexical_score
def test_lexical_score_identical_is_one():
    assert cassette.lexical_score("hello world", "hello world") == pytest.approx(1.0)


def test_lexical_score_empty_expected_is_one():
    # Nothing required -> trivially satisfied (recall over the empty set).
    assert cassette.lexical_score("anything at all", "") == 1.0


def test_lexical_score_containment_beats_ratio():
    # `expected`'s words are all present in a longer `actual` -> containment == 1.0.
    assert cassette.lexical_score("we have processed your refund today", "refund processed") == 1.0


def test_lexical_score_disjoint_is_low():
    assert cassette.lexical_score("the weather is sunny", "quantum entanglement theory") < 0.3


# --------------------------------------------------------------------------- cosine
def test_cosine_opposite_vectors_is_minus_one():
    assert cassette.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_length_mismatch_is_zero():
    assert cassette.cosine([1.0, 2.0, 3.0], [1.0, 2.0]) == 0.0


def test_cosine_zero_vector_is_zero():
    assert cassette.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# --------------------------------------------------------------------------- embedding_scorer (BYO)
def test_embedding_scorer_clamps_negative_cosine_to_zero():
    # Opposed vectors -> cosine -1 -> clamped to 0.0 (a score is always in [0, 1]).
    scorer = cassette.embedding_scorer(lambda texts: [[1.0, 0.0], [-1.0, 0.0]])
    assert scorer("a", "b") == 0.0


def test_embedding_scorer_identical_is_one():
    scorer = cassette.embedding_scorer(lambda texts: [[0.3, 0.7], [0.3, 0.7]])
    assert scorer("x", "y") == pytest.approx(1.0)


def test_embedding_scorer_too_few_vectors_is_zero():
    scorer = cassette.embedding_scorer(lambda texts: [[1.0, 0.0]])  # only one vector back
    assert scorer("a", "b") == 0.0


# ----------------------------------------------------------------- openai_embedding_scorer
def test_openai_embedding_scorer_over_mock_client():
    class _Item:
        def __init__(self, embedding):
            self.embedding = embedding

    class _Embeddings:
        def __init__(self):
            self.calls = []

        def create(self, *, model, input):  # noqa: A002 - OpenAI's kwarg name
            self.calls.append((model, list(input)))
            # Return identical vectors so the cosine is 1.0 (both strings "embed the same").
            return type("Resp", (), {"data": [_Item([1.0, 2.0, 3.0]), _Item([1.0, 2.0, 3.0])]})()

    embeddings = _Embeddings()
    client = type("Client", (), {"embeddings": embeddings})()
    scorer = cassette.openai_embedding_scorer(client, model="text-embedding-3-small")
    assert scorer("refund issued", "we processed your refund") == pytest.approx(1.0)
    assert embeddings.calls == [
        ("text-embedding-3-small", ["refund issued", "we processed your refund"])
    ]


# --------------------------------------------------------------------------- local_embedding_scorer
def test_local_embedding_scorer_without_extra_raises_helpful_error():
    try:
        import model2vec  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("model2vec installed; the missing-extra branch can't be exercised here")
    with pytest.raises(ImportError, match="embeddings"):
        cassette.local_embedding_scorer()


# --------------------------------------------------------------------------- semantic_drift edges
def test_semantic_drift_empty_buffer_is_empty():
    cassette._drift.clear()
    assert cassette.semantic_drift() == []


def test_semantic_drift_accepts_custom_scorer():
    cassette._drift.clear()
    cassette._drift.append({"request_hash": "h", "kind": "llm", "recorded": "a", "live": "b"})
    # A scorer that always returns 0.0 -> every divergence is "meaningful" (below any threshold).
    out = cassette.semantic_drift(threshold=0.5, scorer=lambda a, e: 0.0)
    assert len(out) == 1 and out[0]["score"] == 0.0
    cassette._drift.clear()


# --------------------------------------------------------------------------- promote() line shapes
def test_promote_skips_meta_and_unrecognized_lines(tmp_path):
    trace = tmp_path / "run.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"_meta": {"schema": "acttrace-chain/1"}}),  # skipped: _meta line
                "",  # skipped: blank line
                json.dumps({"kind": "note", "request": {"x": 1}}),  # skipped: unknown kind
                json.dumps({"kind": "llm", "request": "not-a-dict"}),  # skipped: request not a dict
                json.dumps(
                    {
                        "kind": "llm",
                        "request": {"provider": "openai", "model": "gpt-4o", "messages": []},
                        "response": {"choices": [{"message": {"content": "hi"}}]},
                    }
                ),
                json.dumps(
                    {
                        "kind": "tool",
                        "request": {"name": "search", "arguments": {"q": "cats"}},
                        "result": {"hits": 3},  # tool uses `result`, not `response`
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    dst = tmp_path / "cass.json"
    written = cassette.promote(str(trace), to=str(dst))
    assert written == 2  # only the two valid llm/tool lines

    payload = json.loads(dst.read_text(encoding="utf-8"))
    kinds = [e["kind"] for e in payload["entries"]]
    assert kinds == ["llm", "tool"]
    assert payload["entries"][1]["response"] == {"hits": 3}  # the tool `result` became `response`
