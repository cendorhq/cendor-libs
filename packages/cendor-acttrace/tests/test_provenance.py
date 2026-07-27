"""A chain names the format it implements and the library that opened it (D8).

Until now an audit file could not say what produced it — neither the wire format it implements nor
the acttrace version that wrote it. That matters because Cendor's stated policy is that it never
upgrades you: evidence you cannot tie to a known producer is weaker evidence.

The two fields ride INSIDE the ``audit_open`` payload, which makes them part of the hashed chain (so
they cannot be edited after the fact) and — critically — changes nothing about how hashes are
computed. ``_chain_hash`` still covers exactly ``{seq, ts, type, payload}``, so:

  * chains written before this release verify UNCHANGED, and
  * a file holding both old and new entries verifies end to end.

That is the whole reason this is not a format break. Putting the version at the TOP level of an
entry instead would have changed the hashed body for EVERY entry and invalidated every chain in
existence — the design the spec's old §8.8 warned about.

Offline; no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cendor.acttrace import CHAIN_FORMAT, AuditLog, verify


@pytest.fixture(autouse=True)
def _clean_bus():
    from cendor.core import bus

    bus._reset()
    yield
    bus._reset()


def _entries(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "_meta" not in row:
            rows.append(row)
    return rows


def test_audit_open_names_the_format_and_the_producer(tmp_path: Path) -> None:
    path = tmp_path / "chain.jsonl"
    log = AuditLog(system="checkout-agent", path=str(path))
    log.detach()

    first = _entries(path)[0]
    assert first["type"] == "audit_open"
    payload = first["payload"]

    # The format the writer implements — identical in BOTH languages, so it strengthens the
    # cross-language guarantee rather than straining it.
    assert payload["format"] == CHAIN_FORMAT == "acttrace-chain/1"

    # The producer is "<distribution>/<version>". It legitimately DIFFERS between languages
    # (cendor-acttrace/… vs @cendor/acttrace/…) — they are separate packages on independent version
    # lines — so this asserts the SHAPE, never a literal that would couple the two ports.
    assert payload["producer"].startswith("cendor-acttrace/")
    assert payload["producer"].split("/", 1)[1]  # a non-empty version

    # The pre-existing fields are untouched.
    assert payload["system"] == "checkout-agent"

    ok, detail = verify(str(path))
    assert ok, detail


def test_provenance_is_inside_the_hashed_chain(tmp_path: Path) -> None:
    """Tampering with the producer must break verification — else it is decoration, not evidence."""
    path = tmp_path / "chain.jsonl"
    AuditLog(system="s", path=str(path)).detach()

    lines = path.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.strip() and "_meta" not in json.loads(ln))
    row = json.loads(lines[idx])
    row["payload"]["producer"] = "cendor-acttrace/99.99.99"  # a lie about what wrote this file
    lines[idx] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, detail = verify(str(path))
    assert not ok
    assert "tampered" in detail


def test_a_pre_provenance_chain_still_verifies(tmp_path: Path) -> None:
    """THE compatibility guarantee: files written before this release must verify untouched.

    Built by writing a real chain and stripping the two new fields back out, then recomputing that
    one entry's hash the way an older acttrace would have — i.e. exactly the bytes an older
    version produced. If the hash formula had changed, this would fail, and so would every chain
    in the wild.
    """
    from cendor.acttrace import _canonical, _chain_hash  # noqa: PLC2701 - asserting the wire format

    path = tmp_path / "chain.jsonl"
    log = AuditLog(system="legacy", path=str(path))
    log.flag("a second entry, so the chain has more than the opener")
    log.detach()

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = [json.loads(ln) for ln in lines]

    # Rewrite entry 0 as an OLD writer would have emitted it, then re-link the chain from there.
    prev = "0" * 64
    for row in rows:
        if "_meta" in row:
            continue
        if row["type"] == "audit_open":
            row["payload"].pop("format", None)
            row["payload"].pop("producer", None)
        row["prev_hash"] = prev
        row["hash"] = _chain_hash(prev, row["seq"], row["ts"], row["type"], row["payload"])
        prev = row["hash"]

    old = tmp_path / "old.jsonl"
    old.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    ok, detail = verify(str(old))
    assert ok, f"a pre-provenance chain must still verify: {detail}"
    assert _canonical({"a": 1}) == '{"a":1}'  # canonicalization itself is unchanged


def test_mixed_old_and_new_entries_verify(tmp_path: Path) -> None:
    """A chain opened by an older acttrace and appended to by this one verifies end to end.

    This is what per-entry hashing buys: one entry's payload shape never constrains another's.
    """
    from cendor.acttrace import _chain_hash  # noqa: PLC2701

    path = tmp_path / "chain.jsonl"
    log = AuditLog(system="s", path=str(path))
    log.detach()

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = [json.loads(ln) for ln in lines if "_meta" not in json.loads(ln)]
    # Strip provenance from the opener only — as if an OLD writer had created the file …
    rows[0]["payload"].pop("format", None)
    rows[0]["payload"].pop("producer", None)
    rows[0]["prev_hash"] = "0" * 64
    rows[0]["hash"] = _chain_hash(
        rows[0]["prev_hash"], rows[0]["seq"], rows[0]["ts"], rows[0]["type"], rows[0]["payload"]
    )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    # … then this version reopens it and appends. The resume path writes no second audit_open.
    reopened = AuditLog(system="s", path=str(path))
    reopened.flag("appended by the NEW writer, after an OLD opener")
    reopened.detach()

    entries = _entries(path)
    assert "format" not in entries[0]["payload"], "the old opener must stay as it was written"
    ok, detail = verify(str(path))
    assert ok, f"a mixed old/new chain must verify: {detail}"


def test_producer_is_omitted_when_the_version_is_unknown(tmp_path: Path, monkeypatch) -> None:
    """Never invent a version. An unknown producer is OMITTED, following the format's existing rule
    for optional fields — a guessed version inside signed evidence is worse than no version."""
    import cendor.acttrace as at

    monkeypatch.setattr(at, "_producer", lambda: None)

    path = tmp_path / "chain.jsonl"
    AuditLog(system="s", path=str(path)).detach()

    payload = _entries(path)[0]["payload"]
    assert "producer" not in payload
    assert payload["format"] == CHAIN_FORMAT  # the format is always knowable, so it stays
    ok, detail = verify(str(path))
    assert ok, detail
