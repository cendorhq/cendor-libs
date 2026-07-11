"""Benchmark: cendor.squeeze — compression ratio, reversibility, throughput.

Effectiveness: how much each content kind shrinks (by characters *and* by tokens), and whether
``handle.expand()`` returns the original byte-for-byte every time (the headline guarantee). Speed:
``compress()`` throughput in MB/s.
"""

from __future__ import annotations

from _data import code_sample, noisy_logs, noisy_logs_mixed, prose_doc, verbose_json
from _harness import Result, isolated, pct, timed
from cendor.core import tokens
from cendor.squeeze import compress


def _ratio_chars(original: str, small: str) -> float:
    return 1.0 - len(small) / len(original) if original else 0.0


def _ratio_tokens(original: str, small: str, model: str = "gpt-4o") -> float:
    before = tokens.count(original, model)
    after = tokens.count(small, model)
    return 1.0 - after / before if before else 0.0


def run() -> list[Result]:
    rows: list[Result] = []
    samples = {
        "JSON": verbose_json(220),
        "Logs (repetitive)": noisy_logs(1200),
        "Logs (mixed-entropy)": noisy_logs_mixed(1200),
        "Code": code_sample(14),
        "Prose": prose_doc(18),
    }

    with isolated():
        reversible = 0
        for label, original in samples.items():
            small, handle = compress(original, kind="auto")
            restored = handle.expand()
            if restored == original:
                reversible += 1
            # One decimal so an extremely-compressible sample reads honestly (e.g. "99.7%", not a
            # misleading "100%"); KB likewise, so a sub-KB result isn't shown as "0 KB".
            note = (
                f"{len(original) / 1024:.1f} KB → {len(small) / 1024:.1f} KB; "
                f"{pct(_ratio_tokens(original, small), 1)} fewer tokens"
            )
            if label == "Code":
                note += " (on representative code — see caveat)"
            rows.append(
                Result(
                    "squeeze", f"{label} compression", pct(_ratio_chars(original, small), 1), note
                )
            )

        rows.append(
            Result(
                "squeeze",
                "Reversibility (expand() == original)",
                f"{reversible}/{len(samples)} exact",
                "every kind restores byte-for-byte from the content-addressed store",
            )
        )

        # Throughput on the JSON sample (a representative structured payload).
        payload = samples["JSON"]
        mb = len(payload.encode("utf-8")) / 1_048_576
        spc = timed(lambda: compress(payload, kind="json"))
        rows.append(
            Result(
                "squeeze",
                "compress() throughput (JSON)",
                f"{mb / spc:.0f} MB/s",
                f"{mb * 1024:.0f} KB payload, {spc * 1e3:.2f} ms/call",
            )
        )

    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:42} {r.value:>16}   {r.note}")
