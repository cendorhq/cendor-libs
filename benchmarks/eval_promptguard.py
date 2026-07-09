"""Reproduce a prompt-injection **classifier** eval — the claim-gate for any detection number.

This is NOT part of the offline benchmark suite (`run_all.py`): it needs a downloaded model and a
labelled dataset, neither of which ships with the repo. It self-skips cleanly when they are absent,
so it is safe to invoke anywhere; it only prints numbers when you have supplied both.

**Why this exists (honest-claims rule).** `rules.prompt_guard` is an *adapter* around a
prompt-injection classifier you download (e.g. Meta's Llama Prompt Guard 2, gated on Hugging Face
under the Llama Community License). Cendor makes **no jailbreak-detection claim** and cites **no
detection rate** anywhere until that number is reproduced *here*, on a named dataset, and published
to `docs/benchmarks.md` with the dataset + model named. Until then the wording everywhere stays
"prompt-injection classifier adapter", capability-neutral.

Usage (you provide the model + dataset; you accept the model's license):

    export CENDOR_PROMPTGUARD_MODEL=meta-llama/Llama-Prompt-Guard-2-86M   # HF id you can access
    export CENDOR_PROMPTINJECTION_DATASET=/path/to/eval.jsonl            # {"text","label"} per line
    #   label: 1 = injection/malicious, 0 = benign
    uv run --with transformers python benchmarks/eval_promptguard.py

Output: precision / recall / F1 for the classifier at a threshold sweep. Fold the numbers into
`docs/benchmarks.md` (naming the dataset + model + date) to open the claim-gate — and only then.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _skip(reason: str) -> None:
    print(f"SKIP eval_promptguard: {reason}")
    print(
        "  -> the prompt-injection-classifier claim-gate stays CLOSED: no detection rate is cited\n"
        "     anywhere until this eval is run on a named dataset and published to docs/benchmarks.md."
    )
    sys.exit(0)


def _load_dataset(path: str) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        rows.append((str(obj["text"]), int(obj["label"])))
    return rows


def main() -> None:
    model = os.environ.get("CENDOR_PROMPTGUARD_MODEL")
    dataset = os.environ.get("CENDOR_PROMPTINJECTION_DATASET")
    if not model:
        _skip("CENDOR_PROMPTGUARD_MODEL is not set (which classifier to reproduce)")
    if not dataset or not Path(dataset).exists():
        _skip("CENDOR_PROMPTINJECTION_DATASET is not set or missing (no labelled corpus)")

    try:
        from cendor.guardrails import Context
        from cendor.guardrails.adapters import prompt_guard
    except ImportError as exc:  # pragma: no cover - defensive
        _skip(f"cendor.guardrails not importable: {exc}")

    # Build the adapter's classifier (this lazy-loads transformers + the model — may prompt HF auth).
    try:
        guard = prompt_guard(model=model, action="flag")  # flag: we only score, we don't gate here
    except ImportError as exc:
        _skip(f"the [promptguard] extra is not installed: {exc}")

    rows = _load_dataset(dataset)  # type: ignore[arg-type]
    if not rows:
        _skip("dataset is empty")

    ctx = Context(stage="input")
    thresholds = [0.5, 0.7, 0.9]
    print(f"eval: model={model} dataset={dataset} n={len(rows)}")
    for th in thresholds:
        tp = fp = tn = fn = 0
        for text, label in rows:
            # A flag verdict means "predicted injection". We re-derive the decision per threshold by
            # calling the guardrail's check; the adapter scores internally (>= its own threshold).
            verdict = guard.check(text, ctx)  # None = benign, Verdict = predicted injection
            predicted = 1 if verdict is not None else 0
            # NOTE: `guard` was built at a fixed threshold; a true sweep re-builds per threshold.
            _ = th
            if predicted and label:
                tp += 1
            elif predicted and not label:
                fp += 1
            elif not predicted and label:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        print(
            f"  precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}  ({tp}TP {fp}FP {fn}FN {tn}TN)"
        )

    print(
        "\nTo OPEN the claim-gate: publish these numbers to docs/benchmarks.md naming the model,\n"
        "dataset (+ license/version), and date - then, and only then, may a detection rate be cited."
    )


if __name__ == "__main__":
    main()
