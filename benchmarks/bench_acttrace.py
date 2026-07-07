"""Benchmark: cendor.acttrace — append/verify throughput, signing cost, tamper detection.

Auto-population is driven by the bus: each emitted ``LLMCall`` becomes a hash-chained entry. We
measure append throughput (in-memory and file-backed), offline ``verify()`` throughput, the cost of
turning on HMAC signing, and that a single edited byte makes ``verify()`` fail.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _harness import Result, isolated, pct, per_s, rate, timed
from cendor.acttrace import AuditLog, verify
from cendor.core import bus
from cendor.core.types import LLMCall, Money, Usage


def _call() -> LLMCall:
    return LLMCall(
        id="c",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "Approve loan application 4471?"}],
        usage=Usage(input_tokens=900, output_tokens=60),
        cost=Money("0.0123"),
        latency_ms=512.0,
    )


def run() -> list[Result]:
    rows: list[Result] = []
    call = _call()

    # In-memory append throughput (isolates the hash-chain + redact cost). The AuditLog isn't bound
    # to a name on purpose — constructing it subscribes it to the bus, which is all we need here.
    with isolated():
        AuditLog(system="bench")
        rows.append(
            Result(
                "acttrace",
                "Append throughput (in-memory)",
                per_s(rate(lambda: bus.emit(call)), "entries"),
                "sha256 chain + default PII redaction per entry",
            )
        )

    # Signing overhead: signed vs unsigned in-memory append.
    with isolated():
        AuditLog(system="bench")
        t_plain = timed(lambda: bus.emit(call))
    with isolated():
        AuditLog(system="bench", signing_key="ops-signing-key")
        t_signed = timed(lambda: bus.emit(call))
    rows.append(
        Result(
            "acttrace",
            "HMAC signing overhead",
            f"+{pct((t_signed - t_plain) / t_plain)}",
            "per-entry HMAC-SHA256 on top of the chain hash",
        )
    )

    with tempfile.TemporaryDirectory() as tmp:
        # File-backed append throughput. AuditLog keeps its append handle open for durability, so
        # each file-backed log is detach()ed once measured — otherwise Windows can't remove the
        # TemporaryDirectory (a file open in this process can't be deleted).
        with isolated():
            fpath = str(Path(tmp) / "audit.jsonl")
            audit = AuditLog(system="bench", path=fpath)
            rows.append(
                Result(
                    "acttrace",
                    "Append throughput (file-backed)",
                    per_s(rate(lambda: bus.emit(call)), "entries"),
                    "flush + fsync a JSONL line per entry on a kept-open handle",
                )
            )
            audit.detach()  # release the kept-open file handle

        # verify() throughput over a real chain.
        with isolated():
            vpath = Path(tmp) / "verify.jsonl"
            vaudit = AuditLog(system="bench", path=str(vpath))
            for _ in range(2000):
                bus.emit(call)
            n = len(vaudit.entries)
            spc = timed(lambda: verify(str(vpath)))
            vaudit.detach()
            rows.append(
                Result(
                    "acttrace",
                    "verify() throughput",
                    per_s(n / spc, "entries"),
                    f"re-walks a {n}-entry chain in {spc * 1e3:.1f} ms",
                )
            )

        # Tamper detection: flip one byte in a payload and confirm verify() fails.
        with isolated():
            tpath = Path(tmp) / "tamper.jsonl"
            taudit = AuditLog(system="bench", path=str(tpath))
            for _ in range(5):
                bus.emit(call)
            ok_before, _ = verify(str(tpath))
            taudit.detach()  # close the handle before rewriting the file on disk
            # Tamper a field acttrace actually records (it logs provider/model/usage/cost, not
            # prompt text): flip one character of the model id in the first llm_call entry.
            text = tpath.read_text(encoding="utf-8")
            tampered = text.replace('"gpt-4o"', '"gpt-4x"', 1)
            tpath.write_text(tampered, encoding="utf-8")
            ok_after, detail = verify(str(tpath))
            rows.append(
                Result(
                    "acttrace",
                    "Tamper detection",
                    "✓ detected" if (ok_before and not ok_after) else "FAILED",
                    "one edited byte → chain hash mismatch → verify() returns False",
                )
            )

    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:42} {r.value:>16}   {r.note}")
