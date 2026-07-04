"""WS-A: ``max_entries`` bounds the in-memory ring; the file stays the complete, verifiable chain.

A long-running audit log must not grow memory per event (the wrapped SDK doesn't). ``max_entries``
caps the retained in-memory window while every entry is still written to the on-disk chain, so
``verify()`` / ``export()`` cover the *full* history. The default (``None``) is unbounded and
byte-identical to previous behaviour. No network.
"""

import json

import pytest
from cendor.acttrace import AuditLog, BoundedMemoryWithoutPathWarning, verify
from cendor.core import bus
from cendor.core.types import LLMCall, Usage


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


def _emit_calls(n: int) -> None:
    """Emit ``n`` clean LLMCalls on the bus (metadata only, so nothing auto-flags)."""
    for i in range(n):
        bus.emit(
            LLMCall(
                id=f"c{i}",
                provider="openai",
                model="gpt-4o",
                messages=[],
                usage=Usage(input_tokens=10, output_tokens=5),
            )
        )


def test_bounded_memory_evicts_oldest_but_file_stays_complete(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path), max_entries=10)
    try:
        _emit_calls(100)  # + the audit_open entry = 101 appended in total
        assert len(log.entries) == 10  # memory is bounded to the ring size
        assert log.evicted_from_memory == 91  # 101 appended − 10 retained, counted (never silent)
        # the retained window is the *most recent* entries: seqs 91..100
        assert [e.seq for e in log.entries] == list(range(91, 101))
        head = log.head
    finally:
        log.detach()

    # The file holds the whole chain (nothing was dropped from disk) and verifies end-to-end.
    file_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(file_lines) == 101
    ok, detail = verify(str(path), expected_head=head, expect_entries=101)
    assert ok, detail


def test_default_unbounded_retains_all_and_evicted_is_zero(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(system="s", path=str(path))  # max_entries=None (default, unchanged)
    try:
        _emit_calls(50)
        assert len(log.entries) == 51  # audit_open + 50 llm_calls, all retained
        assert log.evicted_from_memory == 0
        assert not any(e.type == "policy_flag" for e in log.entries)  # metadata never auto-flags
        head = log.head
    finally:
        log.detach()
    ok, detail = verify(str(path), expected_head=head, expect_entries=51)
    assert ok, detail


def test_bounded_export_reads_the_full_chain_from_the_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    pack = tmp_path / "pack.jsonl"
    log = AuditLog(system="s", path=str(path), max_entries=5)
    try:
        _emit_calls(40)  # 41 appended; only 5 retained in memory
        assert len(log.entries) == 5
        log.export(str(pack), framework="eu_ai_act")
        head = log.head
    finally:
        log.detach()

    rows = [json.loads(ln) for ln in pack.read_text(encoding="utf-8").splitlines() if ln.strip()]
    meta = rows[0]["_meta"]
    assert meta["entries"] == 41  # the *full* chain, not the 5 retained in memory
    assert meta["head_hash"] == head
    assert meta["summary"]["llm_calls"] == 40
    assert len([r for r in rows if "_meta" not in r]) == 41  # every chain entry exported
    ok, detail = verify(str(pack))  # the exported pack itself verifies
    assert ok, detail


def test_bounded_without_path_warns():
    with pytest.warns(BoundedMemoryWithoutPathWarning):
        log = AuditLog(system="s", max_entries=5)
    log.detach()


def test_max_entries_must_be_positive():
    with pytest.raises(ValueError):
        AuditLog(system="s", max_entries=0)
    with pytest.raises(ValueError):
        AuditLog(system="s", max_entries=-3)


def test_bounded_log_still_signs_and_verifies_full_chain(tmp_path):
    # Bounding memory must not weaken the signed-completeness guarantee: the exported pack's signed
    # _meta and per-entry signatures still cover the full on-disk chain.
    path = tmp_path / "audit.jsonl"
    pack = tmp_path / "pack.jsonl"
    log = AuditLog(system="s", path=str(path), max_entries=3, signing_key="k")
    try:
        _emit_calls(20)  # 21 appended, 3 retained
        log.export(str(pack), framework="eu_ai_act")
    finally:
        log.detach()
    ok, detail = verify(str(pack), key="k")
    assert ok, detail
    assert "metadata signature verified" in detail
