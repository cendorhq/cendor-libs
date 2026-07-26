"""One **live** ``AuditLog`` per chain file — a second one silently corrupted the evidence.

Red-first. Reopening a path is supported and has been since 1.2.2 (``test_resume.py`` covers it): a
process restarts, constructs an ``AuditLog`` over the same path, and the chain resumes from the last
on-disk entry. What was never guarded is **two logs alive at the same time on one path**. Both are
subscribed to the process-global bus, so one ``LLMCall`` is auto-captured *twice*, and each log
appends at its own ``_seq``/``_head`` — which are identical right after the reopen. Two chains
interleave into one file and ``verify()`` reports ``broken link at seq N: prev_hash mismatch``.

Measured in `plan/evidence-cendor-libs-ripple-2026-07-26/repro_f2_double_writer.py`: nothing
restarts from GENESIS (the written-up cause was wrong), the file simply gains a **duplicate seq**.
A real restart — detach, reopen, append — verifies green.

So the fix is to refuse the second live writer at construction, turning an audit-time evidence
failure into a construction-time error. Offline; no network.
"""

import json
from decimal import Decimal

import pytest
from cendor.acttrace import AuditLog, verify
from cendor.core import bus
from cendor.core.types import LLMCall, Money, Usage


@pytest.fixture(autouse=True)
def _clean_bus():
    bus._reset()
    yield
    bus._reset()


def _llm_event(i: int) -> LLMCall:
    call = LLMCall(
        id=f"e{i}",
        provider="openai",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
    )
    call.usage = Usage(10, 2)
    call.cost = Money(Decimal("0.0000027"))
    return call


def _rows(path) -> list[dict]:
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [r for r in rows if "_meta" not in r]


def test_a_second_live_log_on_one_path_is_refused(tmp_path):
    path = tmp_path / "audit.jsonl"
    log1 = AuditLog(system="s", path=str(path), mirror=False)
    try:
        with pytest.raises(ValueError) as excinfo:
            AuditLog(system="s", path=str(path), mirror=False)
        message = str(excinfo.value)
        assert "detach" in message, f"the error must name the way out, got: {message}"
        assert str(path) in message or path.name in message
    finally:
        log1.detach()

    # the refusal must not have damaged the first log: it still appends and still verifies
    bus.emit(_llm_event(1))
    ok, detail = verify(str(path))
    assert ok, detail


def test_the_refusal_is_what_keeps_verify_green(tmp_path):
    # The regression this fix exists for: two live logs produced a duplicate seq and a broken link.
    path = tmp_path / "audit.jsonl"
    log1 = AuditLog(system="s", path=str(path), mirror=False)
    try:
        bus.emit(_llm_event(1))
        bus.emit(_llm_event(2))
        with pytest.raises(ValueError):
            AuditLog(system="s", path=str(path), mirror=False)
        bus.emit(_llm_event(3))
    finally:
        log1.detach()

    rows = _rows(path)
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(set(seqs)), f"a duplicate seq means two writers got through: {seqs}"
    ok, detail = verify(str(path))
    assert ok, detail


def test_detach_releases_the_path(tmp_path):
    path = tmp_path / "audit.jsonl"
    log1 = AuditLog(system="s", path=str(path), mirror=False)
    log1.detach()

    log2 = AuditLog(system="s", path=str(path), mirror=False)  # the real restart case: allowed
    try:
        assert log2.head == log1.head  # a pure resume, no fresh audit_open
        bus.emit(_llm_event(1))
    finally:
        log2.detach()

    log3 = AuditLog(system="s", path=str(path), mirror=False)  # and again, indefinitely
    log3.detach()

    ok, detail = verify(str(path))
    assert ok, detail
    assert sum(1 for r in _rows(path) if r["type"] == "audit_open") == 1


def test_detach_is_idempotent_and_does_not_over_release(tmp_path):
    path = tmp_path / "audit.jsonl"
    log1 = AuditLog(system="s", path=str(path), mirror=False)
    log1.detach()
    log1.detach()  # idempotent, and must not leave the path double-released

    log2 = AuditLog(system="s", path=str(path), mirror=False)
    try:
        with pytest.raises(ValueError):
            AuditLog(system="s", path=str(path), mirror=False)
    finally:
        log2.detach()


def test_different_paths_are_independent(tmp_path):
    a = AuditLog(system="a", path=str(tmp_path / "a.jsonl"), mirror=False)
    b = AuditLog(system="b", path=str(tmp_path / "b.jsonl"), mirror=False)
    try:
        bus.emit(_llm_event(1))
    finally:
        a.detach()
        b.detach()
    for name in ("a.jsonl", "b.jsonl"):
        ok, detail = verify(str(tmp_path / name))
        assert ok, f"{name}: {detail}"


def test_the_same_path_written_two_ways_is_still_one_path(tmp_path):
    # `./sub/../audit.jsonl` and `audit.jsonl` are one file; the guard resolves before comparing.
    path = tmp_path / "audit.jsonl"
    (tmp_path / "sub").mkdir()
    alias = tmp_path / "sub" / ".." / "audit.jsonl"
    log1 = AuditLog(system="s", path=str(path), mirror=False)
    try:
        with pytest.raises(ValueError):
            AuditLog(system="s", path=str(alias), mirror=False)
    finally:
        log1.detach()


def test_path_less_logs_are_never_registered(tmp_path):
    # An in-memory log has no file to corrupt — any number may coexist.
    a = AuditLog(system="a", mirror=False)
    b = AuditLog(system="b", mirror=False)
    c = AuditLog(system="c", mirror=False)
    try:
        bus.emit(_llm_event(1))
        assert all(len(log.entries) >= 2 for log in (a, b, c))
    finally:
        for log in (a, b, c):
            log.detach()


def test_export_neither_claims_nor_releases_a_writer_slot(tmp_path):
    # export() writes its own artifact through a separate truncating writer. It must not take a slot
    # (nor release the live log's), so the chain path is still reopenable after a normal detach.
    # (An export pack itself is deliberately NOT reopenable as a log — a different, older guard.)
    path = tmp_path / "audit.jsonl"
    pack = tmp_path / "pack.jsonl"
    log = AuditLog(system="s", path=str(path), mirror=False)
    try:
        bus.emit(_llm_event(1))
        log.export(str(pack), framework="eu_ai_act")
        with pytest.raises(ValueError):  # the slot is still held by `log`
            AuditLog(system="s", path=str(path), mirror=False)
    finally:
        log.detach()

    reopened = AuditLog(system="s", path=str(path), mirror=False)  # released as normal
    reopened.detach()
    assert pack.exists()
