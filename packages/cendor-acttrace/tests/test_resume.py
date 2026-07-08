"""Reopening ``AuditLog(path=...)`` resumes the on-disk chain — it must never truncate it.

Regression for a HIGH-severity retention bug: the constructor used to open the file in write mode
(``"w"``) and reset the chain to GENESIS, so reopening an existing log wiped every prior entry. The
fix reopens in APPEND mode, rehydrates head/seq/ring from the file, and emits no fresh
``audit_open`` (a pure resume). A corrupt file raises rather than silently restarting from genesis.
Offline; no network.
"""

import json

import pytest
from cendor.acttrace import GENESIS, AuditLog, verify


@pytest.fixture(autouse=True)
def _clean_bus():
    from cendor.core import bus

    bus._reset()
    yield
    bus._reset()


def _write_entries(log: AuditLog, n: int, start: int = 0) -> None:
    """Append ``n`` deterministic policy_flag entries (clean reasons ⇒ nothing auto-flags)."""
    for i in range(n):
        log.flag(f"event-{start + i}")


def _entries_on_disk(path) -> list[dict]:
    """Every chain entry actually persisted to the file (skipping any _meta header)."""
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [r for r in rows if "_meta" not in r]


def test_reopen_appends_and_resumes_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    n, m = 6, 4

    log1 = AuditLog(system="s", path=str(path))
    try:
        _write_entries(log1, n)  # audit_open (seq 0) + n flags = n + 1 entries
    finally:
        log1.detach()
    assert len(_entries_on_disk(path)) == n + 1  # sanity: prior entries are on disk

    # Reconstruct against the SAME path and add more — the prior entries must survive.
    log2 = AuditLog(system="s", path=str(path))
    try:
        assert len(_entries_on_disk(path)) == n + 1  # construction did NOT truncate the file
        _write_entries(log2, m, start=n)
        head = log2.head
    finally:
        log2.detach()

    # (a) the file (source of truth) holds N + M entries — asserted on disk, not just in memory.
    disk = _entries_on_disk(path)
    assert len(disk) == n + 1 + m

    # A pure resume emits exactly ONE audit_open (the original), never a second on reopen.
    assert sum(1 for r in disk if r["type"] == "audit_open") == 1
    assert disk[0]["type"] == "audit_open" and disk[0]["seq"] == 0

    # seq stayed monotonic across the reopen (no reset, no gap, no duplicate).
    assert [r["seq"] for r in disk] == list(range(n + 1 + m))

    # (b) verify() passes GENESIS -> head across the whole chain (old + resumed entries).
    ok, detail = verify(str(path), expected_head=head, expect_entries=n + 1 + m)
    assert ok, detail
    assert disk[0]["prev_hash"] == GENESIS  # chain still roots at genesis


def test_reopen_preserves_chain_via_export(tmp_path):
    # Assert the resumed chain through the public export() surface (re-reads the file).
    path = tmp_path / "audit.jsonl"
    pack = tmp_path / "pack.jsonl"

    log1 = AuditLog(system="s", path=str(path))
    try:
        _write_entries(log1, 5)  # 6 entries incl. audit_open
    finally:
        log1.detach()

    log2 = AuditLog(system="s", path=str(path))
    try:
        _write_entries(log2, 3, start=5)  # 9 entries total
        head = log2.head
        log2.export(str(pack))
    finally:
        log2.detach()

    rows = [json.loads(ln) for ln in pack.read_text(encoding="utf-8").splitlines() if ln.strip()]
    meta = rows[0]["_meta"]
    assert meta["entries"] == 9  # full chain, old + resumed
    assert meta["head_hash"] == head
    assert len([r for r in rows if "_meta" not in r]) == 9
    ok, detail = verify(str(pack))  # the exported pack verifies end-to-end
    assert ok, detail


def test_fresh_log_emits_audit_open_as_seq0(tmp_path):
    # A brand-new (nonexistent) path keeps today's behaviour: audit_open is entry seq 0.
    path = tmp_path / "does_not_exist_yet.jsonl"
    assert not path.exists()

    log = AuditLog(system="s", path=str(path))
    try:
        assert log.entries[0].type == "audit_open"
        assert log.entries[0].seq == 0
        assert log.entries[0].prev_hash == GENESIS
        head = log.head
    finally:
        log.detach()

    disk = _entries_on_disk(path)
    assert disk[0]["type"] == "audit_open" and disk[0]["seq"] == 0
    ok, detail = verify(str(path), expected_head=head, expect_entries=1)
    assert ok, detail


def test_reopen_corrupt_file_raises_instead_of_truncating(tmp_path):
    # A corrupt trailing line must raise — never a silent restart from GENESIS (the retention bug).
    path = tmp_path / "audit.jsonl"
    log1 = AuditLog(system="s", path=str(path))
    try:
        _write_entries(log1, 3)
    finally:
        log1.detach()

    before = path.read_text(encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json at all\n")  # simulate a crash mid-write / corruption

    with pytest.raises(ValueError, match="corrupt"):
        AuditLog(system="s", path=str(path))

    # The prior chain is untouched — construction failed without truncating a single byte.
    assert path.read_text(encoding="utf-8").startswith(before)


def test_reopen_bounded_rehydrates_tail_and_exports_full_chain(tmp_path):
    # Reopening with max_entries loads only the tail into the ring, marks the rest evicted, and
    # export() still re-reads the full chain from the file.
    path = tmp_path / "audit.jsonl"
    pack = tmp_path / "pack.jsonl"

    log1 = AuditLog(system="s", path=str(path))
    try:
        _write_entries(log1, 20)  # audit_open + 20 = 21 entries on disk
    finally:
        log1.detach()

    log2 = AuditLog(system="s", path=str(path), max_entries=5)
    try:
        assert len(log2.entries) == 5  # only the tail is held in memory
        assert log2.evicted_from_memory == 16  # 21 on disk - 5 retained
        _write_entries(log2, 4, start=20)  # 25 total; ring stays at 5
        head = log2.head
        log2.export(str(pack))
    finally:
        log2.detach()

    rows = [json.loads(ln) for ln in pack.read_text(encoding="utf-8").splitlines() if ln.strip()]
    meta = rows[0]["_meta"]
    assert meta["entries"] == 25  # the full chain from disk, not the 5 retained in memory
    assert meta["head_hash"] == head
    ok, detail = verify(str(pack))
    assert ok, detail
    ok, detail = verify(str(path), expected_head=head, expect_entries=25)
    assert ok, detail
