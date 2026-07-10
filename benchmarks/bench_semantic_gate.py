"""Reproduce a **semantic-gate** eval — the claim-gate for any paraphrase/topic catch-rate number.

This is NOT part of the offline benchmark suite (`run_all.py`): it needs a downloaded embedding
model (the `[embeddings]` extra, model2vec) and, for a *publishable* number, a **named public
corpus** you supply. It self-skips cleanly when the model is absent, so it is safe to invoke
anywhere; with no `--corpus` it runs a tiny built-in *smoke* set that demonstrates the harness but is
explicitly **not** a corpus you may cite.

**Why this exists (honest-claims rule).** `rules.custom_category` / `rules.denied_topics` /
`rules.intent` (embedding backend) are similarity heuristics over a bring-your-own embedder. Cendor
cites **no paraphrase/topic catch-rate** anywhere until a rate is reproduced *here*, on a *named
public corpus*, and published to `docs/benchmarks.md` with the corpus + model + threshold + date.
Until then the wording everywhere stays "a tuned heuristic — calibrate the threshold", claim-neutral.

Usage (you provide the embedder via the extra; optionally a real corpus):

    uv run --with model2vec python benchmarks/bench_semantic_gate.py
    # or with a real corpus (JSONL: {"text": ..., "category": ..., "label": 0|1} per line;
    #   label 1 = should trip for that category, 0 = benign):
    CENDOR_SEMANTIC_CORPUS=/path/to/eval.jsonl uv run --with model2vec python benchmarks/bench_semantic_gate.py

Output: per-threshold **trip rate** (recall on the should-trip set) and **false-positive rate** (on
the benign set), so you can pick a threshold. Fold real numbers into `docs/benchmarks.md` (naming the
corpus + model + date) to open the claim-gate — and only then.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# A tiny SMOKE set: category → (should-trip paraphrases, benign lines). NOT a corpus to cite — it
# only shows the harness runs. Real numbers require a named public corpus via CENDOR_SEMANTIC_CORPUS.
_SMOKE = {
    "code_requests": {
        "examples": ["write a program", "build an app", "create a script"],
        "trip": ["make me a hello-world app", "can you code a small tool", "write some python for me"],
        "benign": ["what is the capital of france", "summarise this article", "book a flight to oslo"],
    },
    "medical_advice": {
        "examples": ["medical diagnosis", "prescribe medication", "treat a symptom"],
        "trip": ["what pills should I take for a fever", "diagnose my chest pain", "how do I treat an infection"],
        "benign": ["what is the weather today", "write a poem about the sea", "explain recursion"],
    },
}


def _skip(reason: str) -> None:
    print(f"SKIP bench_semantic_gate: {reason}")
    print(
        "  -> the semantic-gate claim-gate stays CLOSED: no paraphrase/topic catch-rate is cited\n"
        "     anywhere until this eval is run on a NAMED PUBLIC corpus and published to\n"
        "     docs/benchmarks.md (corpus + model + threshold + date)."
    )
    sys.exit(0)


def _load_corpus(path: str) -> dict[str, dict[str, list[str]]]:
    """A JSONL corpus → the same {category: {examples, trip, benign}} shape. `examples` come from
    lines you tag as the category's exemplars (label 1 with a leading `exemplar: true`), or you can
    pass a companion `.examples.json`. Kept intentionally simple — adapt to your dataset."""
    cats: dict[str, dict[str, list[str]]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        cat = str(obj["category"])
        bucket = cats.setdefault(cat, {"examples": [], "trip": [], "benign": []})
        if obj.get("exemplar"):
            bucket["examples"].append(str(obj["text"]))
        elif int(obj["label"]) == 1:
            bucket["trip"].append(str(obj["text"]))
        else:
            bucket["benign"].append(str(obj["text"]))
    return cats


def main() -> None:
    try:
        from cendor.guardrails import Context, embeddings, rules
    except ImportError as exc:  # pragma: no cover - defensive
        _skip(f"cendor.guardrails not importable: {exc}")

    try:
        embed = embeddings.local_embedder()
        embed("warmup")  # force the (lazy) model load now so a missing extra skips cleanly
    except ImportError as exc:
        _skip(f"the [embeddings] extra is not installed: {exc}")

    corpus_path = os.environ.get("CENDOR_SEMANTIC_CORPUS")
    if corpus_path and Path(corpus_path).exists():
        corpus = _load_corpus(corpus_path)
        named = corpus_path
    else:
        corpus = _SMOKE
        named = "BUILT-IN SMOKE SET (do NOT cite — replace with a named public corpus)"

    ctx = Context(stage="input")
    thresholds = [0.5, 0.6, 0.7, 0.8, 0.9]
    print(f"eval: corpus={named}  categories={list(corpus)}")
    for th in thresholds:
        trips = trip_total = fp = benign_total = 0
        for cat, data in corpus.items():
            if not data["examples"]:
                continue
            rule = rules.custom_category(cat, data["examples"], embed=embed, threshold=th, action="flag")
            for text in data.get("trip", []):
                trip_total += 1
                if rule.check(text, ctx) is not None:
                    trips += 1
            for text in data.get("benign", []):
                benign_total += 1
                if rule.check(text, ctx) is not None:
                    fp += 1
        trip_rate = trips / trip_total if trip_total else 0.0
        fpr = fp / benign_total if benign_total else 0.0
        print(f"  threshold={th:.2f}  trip_rate={trip_rate:.3f}  false_positive_rate={fpr:.3f}")

    print(
        "\nTo OPEN the claim-gate: run this on a NAMED PUBLIC corpus and publish the numbers to\n"
        "docs/benchmarks.md (corpus + license/version, model, threshold, date) - then, and only\n"
        "then, may a paraphrase/topic catch-rate be cited. The built-in smoke set is not citable."
    )


if __name__ == "__main__":
    main()
