"""Compression is content-aware, deterministic, and 100% reversible. No network."""

import json

import pytest
from cendor.core import protocols, tokens
from cendor.squeeze import SqueezeCompressor, compress, decompress, detect


@pytest.fixture(autouse=True)
def _heuristic_tokens(monkeypatch):
    monkeypatch.setattr(tokens, "_tiktoken_encoding", lambda model: None)
    yield


def test_detect():
    assert detect('{"a": 1}') == "json"
    assert detect("[1, 2, 3]") == "json"
    logs = "\n".join(f"2026-06-01T00:00:0{i} INFO started worker" for i in range(5))
    assert detect(logs) == "logs"
    assert detect("The cat sat on the mat. It was a sunny day.") == "prose"


def test_json_compression_is_smaller_and_reversible():
    obj = {"name": "alice", "age": 30, "note": None, "tags": ["x", "y"], "extra": None}
    pretty = json.dumps(obj, indent=4)
    small, handle = compress(pretty, kind="auto")
    assert detect(pretty) == "json"
    assert len(small) < len(pretty)  # whitespace + nulls gone
    assert "note" not in small and "null" not in small  # nulls dropped
    assert handle.expand() == pretty  # exact original restored


def test_logs_dedup_collapses_repeats():
    logs = "\n".join(["2026-06-01T00:00:00Z INFO retry attempt"] * 20)
    small, handle = compress(logs, kind="logs")
    assert "(×20)" in small
    assert tokens.count(small, "gpt-4o") < tokens.count(logs, "gpt-4o")
    assert handle.expand() == logs


def test_prose_extractive_hits_target_tokens():
    text = (
        "Refunds are processed within five business days. "
        "The weather today is mild and pleasant. "
        "Customers must contact support to request a refund. "
        "Our office cat is named Mittens. "
        "Refund eligibility depends on the purchase date."
    )
    target = 20
    small, handle = compress(text, kind="prose", target_tokens=target)
    assert tokens.count(small, "gpt-4o") <= target
    assert len(small) < len(text)
    assert handle.expand() == text  # original always restorable


def test_object_input_serialized_and_restored():
    small, handle = compress({"k": "v", "n": None}, kind="auto")
    assert "null" not in small
    restored = json.loads(handle.expand())
    assert restored == {"k": "v", "n": None}


def test_decompress_matches_expand():
    small, handle = compress("hello world. goodbye world.", kind="prose")
    assert decompress(handle) == handle.expand()


def test_ccr_store_dedupes_identical_originals():
    import cendor.squeeze as sq

    before = len(sq._backend)
    _, h1 = compress("identical content here", kind="prose")
    _, h2 = compress("identical content here", kind="prose")
    assert h1.original_ref == h2.original_ref  # same hash key
    assert len(sq._backend) == before + 1  # stored once


def test_memory_store_eviction_cap():
    import cendor.squeeze as sq
    from cendor.squeeze.store import MemoryStore

    previous = sq.use_store(MemoryStore(max_items=2))
    try:
        _, h1 = compress("first original content here", kind="prose")
        _, h2 = compress("second original content here", kind="prose")
        _, h3 = compress("third original content here", kind="prose")  # evicts the first
        assert len(sq._backend) == 2
        assert h2.expand() and h3.expand()  # newest two survive
        with pytest.raises(KeyError):
            h1.expand()  # oldest was evicted (documented trade-off of a capped store)
    finally:
        sq.use_store(previous)


def test_sqlite_store_backend_persists_and_expands(tmp_path):
    import cendor.squeeze as sq
    from cendor.squeeze.store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "ccr.db"))
    previous = sq.use_store(store)
    try:
        original = '{"name": "alice", "age": 30, "note": null}'
        small, handle = compress(original, kind="json")
        assert len(small) < len(original)
        assert handle.original_ref in store  # original persisted to SQLite
        assert handle.expand() == original  # restored from the SQLite backend
    finally:
        sq.use_store(previous)
        store.close()


def test_satisfies_core_compressor_protocol():
    assert isinstance(SqueezeCompressor(), protocols.Compressor)
    small, handle = SqueezeCompressor().compress("a. b. c. d.", target_tokens=5, model="gpt-4o")
    assert isinstance(handle.expand(), str)


def test_detect_code():
    src = "def add(a, b):\n    # sum them\n    return a + b\n"
    assert detect(src) == "code"
    js = "function f(x) {\n  return x * 2;\n}\n"
    assert detect(js) == "code"


def test_code_compression_strips_comments_and_is_reversible():
    src = "def add(a, b):\n    # add two numbers\n    return a + b\n\n// trailing\n"
    small, handle = compress(src, kind="code")
    assert "# add two numbers" not in small
    assert "// trailing" not in small
    assert "" not in small.split("\n")  # blank lines gone
    assert "return a + b" in small  # structure kept
    assert handle.expand() == src  # exact original restorable
    assert tokens.count(small, "gpt-4o") < tokens.count(src, "gpt-4o")


def test_code_lossless_keeps_comments():
    src = "def f():\n    # keep me\n    return 1\n"
    small, _ = compress(src, kind="code", fidelity="lossless")
    assert "# keep me" in small  # comments preserved at lossless fidelity


def test_fidelity_dial_on_prose():
    text = ". ".join(f"Sentence {i} about refunds and billing matters" for i in range(12)) + "."
    lossless, _ = compress(text, kind="prose", fidelity="lossless")
    balanced, _ = compress(text, kind="prose", fidelity="balanced")
    aggressive, _ = compress(text, kind="prose", fidelity="aggressive")
    assert lossless == text  # lossless prose is a no-op (whitespace aside)
    t = lambda s: tokens.count(s, "gpt-4o")  # noqa: E731
    assert t(aggressive) <= t(balanced) <= t(lossless)


def test_invalid_fidelity_rejected():
    with pytest.raises(ValueError):
        compress("x", fidelity="ultra")


def test_json_lossless_keeps_nulls():
    small, _ = compress('{"a": 1, "b": null}', kind="json", fidelity="lossless")
    assert "null" in small  # nulls retained at lossless fidelity


def test_prose_never_exceeds_target_even_with_one_dominant_sentence():
    # The top-ranked sentence is always kept; it must still be truncated to honor target_tokens.
    text = (
        "refund refund refund refund refund refund refund refund refund refund policy here. "
        "The cat sat. A dog ran. Birds fly."
    )
    small, handle = compress(text, kind="prose", target_tokens=5)
    assert tokens.count(small, "gpt-4o") <= 5
    assert handle.expand() == text  # still fully reversible


def test_code_comment_stripping_preserves_string_literals():
    src = 'url = "https://example.com/path"  // trailing\nkey = "color #ff0000"\nreturn url\n'
    small, handle = compress(src, kind="code")
    assert "https://example.com/path" in small  # // inside a string is not a comment
    assert "#ff0000" in small  # # inside a string is not a comment
    assert "// trailing" not in small  # the real comment is gone
    assert handle.expand() == src


def test_code_keeps_preprocessor_and_shebang():
    src = "#!/usr/bin/env python\n#include <stdio.h>\nx = 1  # a real comment\nreturn x\n"
    small, _ = compress(src, kind="code")
    assert "#!/usr/bin/env python" in small  # shebang kept
    assert "#include <stdio.h>" in small  # preprocessor directive kept
    assert "# a real comment" not in small  # ordinary # comment stripped


def test_logs_preserve_chronological_order_under_target():
    logs = "\n".join(
        [
            "2026-06-01T00:00:01Z INFO alpha first",
            "2026-06-01T00:00:02Z INFO beta second",
            "2026-06-01T00:00:03Z ERROR gamma",
            "2026-06-01T00:00:04Z ERROR gamma",
            "2026-06-01T00:00:05Z ERROR gamma",
        ]
    )
    small, _ = compress(logs, kind="logs", target_tokens=1000)
    assert small.splitlines()[0].endswith("INFO alpha first")  # chronological, not freq-sorted
    assert tokens.count(small, "gpt-4o") <= 1000


def test_handle_to_dict_round_trips_with_persistent_store():
    import cendor.squeeze as sq
    from cendor.squeeze import Handle
    from cendor.squeeze.store import SQLiteStore

    store = SQLiteStore(":memory:")
    previous = sq.use_store(store)
    try:
        original = '{"a": 1, "b": null}'
        _small, handle = compress(original, kind="json")
        rebuilt = Handle.from_dict(handle.to_dict())  # e.g. loaded from disk next process
        assert rebuilt.expand() == original
        assert rebuilt.technique == handle.technique
        assert len(store) == 1
    finally:
        sq.use_store(previous)
        store.close()


def test_contextkit_compress_routes_through_squeeze():
    # End-to-end: contextkit discovers squeeze by shape via the [squeeze] extra (both installed).
    from cendor.contextkit import Block, Context

    ctx = Context(budget_tokens=30, model="gpt-4o")
    ctx.add(Block("keep me", priority=10, role="system"))
    big = " ".join(f"Sentence number {i} about refunds and billing." for i in range(20))
    ctx.add(Block(big, priority=1, role="user", evict="compress"))
    ctx.assemble()
    decision = next(d for d in ctx.report().decisions if d.role == "user")
    assert decision.action == "compressed"
    assert decision.tokens_after < decision.tokens_before


# --------------------------------------------------------------------------- Phase 2.1 quality


def test_prose_keeps_the_obviously_key_sentence():
    # The key sentence is longer and information-dense (repeated domain terms). Length-normalized
    # scoring must keep it, where the old mean-based score favored short common-word filler.
    key = "The migration corrupts the billing ledger and double-charges every enterprise customer."
    filler = [
        "It was a nice day.",
        "We had a good time.",
        "That is all for now.",
        "So it goes here.",
        "Nothing to see.",
    ]
    text = " ".join([filler[0], filler[1], key, filler[2], filler[3], filler[4]])
    small, _ = compress(text, kind="prose", target_tokens=25, model="gpt-4o")
    assert "double-charges" in small  # the obviously-key sentence survived
    assert "nice day" not in small  # short common-word filler was dropped


def test_prose_does_not_split_on_abbreviations():
    from cendor.squeeze import _split_sentences

    assert _split_sentences("Dr. Smith paid the bill.") == ["Dr. Smith paid the bill."]
    assert _split_sentences("Use e.g. a refund. Then stop.") == [
        "Use e.g. a refund.",
        "Then stop.",
    ]
    assert _split_sentences("It cost 3.5 million dollars. Wow.") == [
        "It cost 3.5 million dollars.",
        "Wow.",
    ]


def test_json_safety_truncate_stays_valid_json():
    # Structural drop keeps the output parseable — the old prefix-cut produced invalid JSON.
    obj = {f"key_{i}": f"value number {i} with some padding text" for i in range(40)}
    original = json.dumps(obj)
    small, handle = compress(original, kind="json", target_tokens=40, model="gpt-4o")
    parsed = json.loads(small)  # must not raise — valid JSON despite the budget
    assert isinstance(parsed, dict)
    assert len(parsed) < len(obj)  # some keys were dropped to fit
    assert handle.expand() == original  # the full original is still restorable


def test_json_list_safety_truncate_stays_valid():
    original = json.dumps([{"i": i, "text": "padding padding padding"} for i in range(40)])
    small, _ = compress(original, kind="json", target_tokens=40, model="gpt-4o")
    parsed = json.loads(small)
    assert isinstance(parsed, list) and 0 < len(parsed) < 40


def test_logs_normalize_ips_hex_and_integers():
    # Lines differing only in an IP, a request id (hex), and a counter must collapse to one pattern.
    lines = [
        "2026-07-02T10:00:00Z GET /x from 10.0.0.1 req=deadbeef12345678 status 200",
        "2026-07-02T10:00:01Z GET /x from 10.0.0.2 req=cafebabe87654321 status 404",
        "2026-07-02T10:00:02Z GET /x from 192.168.1.5 req=0123456789abcdef status 500",
    ]
    small, handle = compress("\n".join(lines), kind="logs")
    assert handle.restore_map["patterns"] == 1  # all three normalized to a single pattern
    assert "(×3)" in small  # deduped with a count
    assert "<ip>" in small and "<hex>" in small and "<n>" in small


def test_memory_store_lru_refreshes_on_get():
    import cendor.squeeze as sq
    from cendor.squeeze.store import MemoryStore

    previous = sq.use_store(MemoryStore(max_items=2))
    try:
        _, h1 = compress("first original content here", kind="prose")
        _, h2 = compress("second original content here", kind="prose")
        h1.expand()  # touch h1 -> now most-recently-used, so h2 is the coldest
        _, h3 = compress("third original content here", kind="prose")  # evicts the LRU (h2)
        assert h1.expand() and h3.expand()  # h1 survived because it was refreshed
        with pytest.raises(KeyError):
            h2.expand()  # h2 was the least-recently-used and got evicted
    finally:
        sq.use_store(previous)


def test_handle_id_is_deterministic():
    # squeeze is deterministic: identical (content, technique) yields the same handle id (no uuid4).
    _, h1 = compress('{"a": 1, "b": null}', kind="json")
    _, h2 = compress('{"a": 1, "b": null}', kind="json")
    assert h1.id == h2.id
    _, other = compress('{"a": 2}', kind="json")
    assert other.id != h1.id  # different content -> different id
