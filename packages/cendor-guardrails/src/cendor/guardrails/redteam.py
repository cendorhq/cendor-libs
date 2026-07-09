"""Red-team evaluation — measure a guardrail's trip rate against a labeled corpus of attacks.

This is the honest path to *any* detection number: run **your** guardrails over a labeled corpus and
publish the per-category trip rate + false-positive rate, naming the corpus. cendor **vends no
attack data** — :func:`load_corpus` reads a local file **you** assembled or downloaded (public sets
like the AdvBench / JailbreakBench / HackAPrompt corpora are referenced in docs; you fetch them
under their own licenses). The report is a measurement tool, not a claim: no catch-rate ships
until it is run on a named corpus and published (docs/guardrails.md "Threat model").

Deterministic guardrails make the run fully offline and reproducible; a run that includes an
``llm_judge`` or a hosted rail should be **cassette-recorded** so CI stays offline (see the cookbook
recipe). Imports only :mod:`.decision` and the engine — no network, no data.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .decision import Guardrail

__all__ = ["AttackCase", "RedTeamReport", "load_corpus", "run_redteam", "run_redteam_async"]

#: A case labeled ``"attack"`` should trip; a case labeled ``"benign"`` should pass. Any other label
#: is counted only in the totals (neither recall nor false-positive), so a mixed corpus is fine.
ATTACK = "attack"
BENIGN = "benign"


@dataclass
class AttackCase:
    """One labeled probe. ``label`` is ``"attack"`` (should trip) or ``"benign"`` (should pass)."""

    text: str
    label: str = ATTACK
    category: str = ""
    id: str = ""


@dataclass
class RedTeamReport:
    """The outcome of a red-team run — counts + rates, and a per-category breakdown.

    ``trip_rate`` is recall on the ``attack`` cases (caught / attacks); ``false_positive_rate`` is
    the fraction of ``benign`` cases that tripped. Both are ``0.0`` when their denominator is 0.
    ``by_category`` maps a category to its ``(attacks, caught)`` counts. No number here is a shipped
    claim — it describes *this* corpus, which you must name when you publish it.
    """

    total: int = 0
    attacks: int = 0
    benign: int = 0
    caught: int = 0  # attack cases that tripped (true positives)
    false_positives: int = 0  # benign cases that tripped
    by_category: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def trip_rate(self) -> float:
        return self.caught / self.attacks if self.attacks else 0.0

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / self.benign if self.benign else 0.0

    def summary(self) -> str:
        """A one-line, corpus-agnostic summary (safe to log — no case text)."""
        return (
            f"{self.total} cases: trip rate {self.trip_rate:.1%} "
            f"({self.caught}/{self.attacks} attacks), "
            f"false-positive rate {self.false_positive_rate:.1%} "
            f"({self.false_positives}/{self.benign} benign)"
        )


def load_corpus(source: str | Path, *, format: str | None = None) -> list[AttackCase]:
    """Load a labeled corpus from a **local** file — ``.jsonl`` (one object per line), ``.json`` (a
    list of objects), or ``.csv`` (a header row). Each record needs a ``text`` field; ``label`` /
    ``category`` / ``id`` are optional (``label`` defaults to ``"attack"``).

    cendor ships no data: point this at a file you assembled or downloaded (e.g. a public
    prompt-injection set, under its own license). ``format`` overrides the extension inference.
    """
    path = Path(source)
    fmt = format or path.suffix.lower().lstrip(".")
    text = path.read_text(encoding="utf-8")
    if fmt == "jsonl":
        rows: Iterable[Any] = (json.loads(line) for line in text.splitlines() if line.strip())
    elif fmt == "json":
        data = json.loads(text)
        rows = data if isinstance(data, list) else [data]
    elif fmt == "csv":
        rows = list(csv.DictReader(text.splitlines()))
    else:
        raise ValueError(f"unsupported corpus format {fmt!r}; use jsonl / json / csv")
    return [_to_case(r) for r in rows]


def _to_case(row: Any) -> AttackCase:
    if not isinstance(row, dict):
        raise ValueError(f"each corpus record must be an object, got {type(row).__name__}")
    if "text" not in row:
        raise ValueError("each corpus record needs a 'text' field")
    return AttackCase(
        text=str(row["text"]),
        label=str(row.get("label", ATTACK)),
        category=str(row.get("category", "")),
        id=str(row.get("id", "")),
    )


def _tally(report: RedTeamReport, case: AttackCase, tripped: bool) -> None:
    report.total += 1
    if case.label == ATTACK:
        report.attacks += 1
        if tripped:
            report.caught += 1
        a, c = report.by_category.get(case.category, (0, 0))
        report.by_category[case.category] = (a + 1, c + (1 if tripped else 0))
    elif case.label == BENIGN:
        report.benign += 1
        if tripped:
            report.false_positives += 1


def run_redteam(
    guardrails: Sequence[Guardrail],
    cases: Iterable[AttackCase],
    *,
    stage: str = "input",
) -> RedTeamReport:
    """Run ``guardrails`` over each case at ``stage`` and tally trip rate + false positives.

    A case "trips" when any guardrail blocks, redacts, or flags it. A ``block`` raises
    :class:`~cendor.guardrails.GuardrailTripped` inside the engine — that counts as a trip, not an
    error. Sync only: for an ``async`` check use :func:`run_redteam_async`.
    """
    from . import GuardrailTripped, apply

    report = RedTeamReport()
    for case in cases:
        try:
            decisions = apply(guardrails, stage, case.text)
            tripped = len(decisions) > 0
        except GuardrailTripped:
            tripped = True
        _tally(report, case, tripped)
    return report


async def run_redteam_async(
    guardrails: Sequence[Guardrail],
    cases: Iterable[AttackCase],
    *,
    stage: str = "input",
) -> RedTeamReport:
    """Async counterpart of :func:`run_redteam` — awaits ``async`` checks (an ``llm_judge`` or a
    hosted rail). Cassette-record the model/cloud calls to keep a CI run offline."""
    from . import GuardrailTripped, apply_async

    report = RedTeamReport()
    for case in cases:
        try:
            decisions = await apply_async(guardrails, stage, case.text)
            tripped = len(decisions) > 0
        except GuardrailTripped:
            tripped = True
        _tally(report, case, tripped)
    return report
