"""Cross-language conformance — reverse direction (JS chain -> Python verify).

The committed golden ``vectors/js_written_chain.jsonl`` is a signed audit chain WRITTEN BY the
JavaScript ``@cendor/acttrace`` (via ``cendor-libs-js/scripts/roundtrip-acttrace.mjs``, HMAC key
``roundtrip-key``). Python's :func:`cendor.acttrace.verify` must accept it — this is the symmetric
partner to the JS CI, which replays Python-written fixtures. Together they make the "conformance
vectors in both CIs" claim true: each language verifies an artifact produced by the other.

Regenerate the vector (only if the wire format changes intentionally) from the cendor-libs-js repo:

    node scripts/roundtrip-acttrace.mjs OUT.jsonl   # then copy OUT.jsonl into tests/vectors/

This guards that Python's canonical-bytes + HMAC verification stays byte-compatible with JS.
"""

from __future__ import annotations

import json
from pathlib import Path

from cendor.acttrace import verify

VECTOR = Path(__file__).parent / "vectors" / "js_written_chain.jsonl"
KEY = "roundtrip-key"


def test_js_written_chain_verifies_in_python() -> None:
    ok, detail = verify(str(VECTOR), key=KEY)
    assert ok, f"JS-written audit chain failed Python verify(): {detail}"
    assert "7 entries" in detail  # 7 hash-chained entries (after the _meta header)


def test_wrong_key_is_rejected() -> None:
    # Proves the signature is genuinely checked cross-language, not just the chain shape.
    ok, _ = verify(str(VECTOR), key="not-the-key")
    assert not ok


def test_tampering_a_js_entry_is_detected(tmp_path: Path) -> None:
    # Corrupt one entry's payload; Python must reject the (now inconsistent) hash chain.
    lines = VECTOR.read_text(encoding="utf-8").splitlines()
    first_entry = json.loads(lines[1])  # line 0 is the _meta header
    first_entry["payload"] = {**first_entry.get("payload", {}), "tampered": True}
    lines[1] = json.dumps(first_entry)
    bad = tmp_path / "tampered.jsonl"
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _ = verify(str(bad), key=KEY)
    assert not ok
