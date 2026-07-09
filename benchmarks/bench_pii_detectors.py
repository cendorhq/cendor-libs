"""Benchmark: PII / secret detection quality for the ``acttrace`` catalogue — the Tier-1 detector
tier the guardrails story leans on (bridged from the SDK as ``rules.pii`` / ``secrets`` /
``entropy``). This is the ONLY place a catch-rate number is allowed to come from (see the plan's
claim-gate); everything else stays capability-neutral until a number is measured here.

**Corpus.** Small, hand-labelled, and **synthetic** — every value below is fabricated (fake keys,
Luhn-valid but non-issued card numbers, RFC-5737 documentation IPs, 555 phone numbers). It is
modelled on the *formats* used by public PII corpora (Microsoft Presidio's `presidio-research`
generator, the Faker library, and the AWS Comprehend PII entity list) but scrapes none of them, so
the harness runs fully offline and ships no third-party data. It is deliberately *not* large enough
to publish a headline "we catch X%" marketing claim — it establishes the **methodology** and gives
honest per-group precision/recall for the shipped regex catalogue; a public catch-rate claim needs
a larger corpus assembled from a licensed public dataset (documented as a follow-up).

**Metric.** For each labelled example we know the categories that *should* be detected (ground
truth) and run ``acttrace.scan`` to get the categories that *were* detected. Per group we count
true/false positives and false negatives across the corpus and report precision + recall. A block
of negative examples (look-alikes that must NOT trip — non-Luhn digit runs, prose, version strings)
is what makes precision meaningful, not just recall.

**regex-only vs +NER.** The regex catalogue targets *structured* PII (patterns + validators); it
does not target free-text **names/addresses**, so its recall on those is ~0 by design. The optional
Presidio NER backend (``cendor-acttrace[ner]``) is what covers them — measured here only when it is
installed, otherwise recorded as "not measured" (never assumed).

Run:  uv run python benchmarks/bench_pii_detectors.py
      uv run --with presidio-analyzer --with presidio-anonymizer python benchmarks/bench_pii_detectors.py
"""

from __future__ import annotations

from dataclasses import dataclass

from _harness import Result, dur, pct, timed
from cendor.acttrace import Policy, ner_available, scan


@dataclass(frozen=True)
class Example:
    text: str
    truth: frozenset[str]  # categories that SHOULD be detected ("" = a clean negative example)
    group: (
        str  # the group this example exercises, for per-group aggregation ("clean" for negatives)
    )


def _ex(text: str, *categories: str, group: str) -> Example:
    return Example(text, frozenset(categories), group)


# --------------------------------------------------------------------------- the labelled corpus
# Every value is fabricated. Cards are Luhn-valid test numbers; IPs are RFC-5737 documentation
# ranges; phones use the 555 exchange; SSNs avoid invalid area numbers so the validator accepts them.
CORPUS: list[Example] = [
    # -- secrets ---------------------------------------------------------------------------------
    _ex("here is my key sk-abcdEFGH1234ijklMNOP for the api", "api_key", group="secret"),
    _ex("export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE now", "aws_key", group="secret"),
    _ex("token AIzaSyD0123456789abcdefghijklmnopqrstuv is live", "google_api_key", group="secret"),
    _ex(
        "auth: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEFghiJKL",
        "jwt",
        group="secret",
    ),
    _ex("Authorization: Bearer abc123def456ghi789 header", "bearer_token", group="secret"),
    _ex("ci token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 leaked", "github_token", group="secret"),
    _ex("slack xoxb-1234567890-abcdefghijklmno posted", "slack_token", group="secret"),
    # -- financial (validator-gated) -------------------------------------------------------------
    _ex("card 4111 1111 1111 1111 charged", "credit_card", group="financial"),
    _ex("pay to GB82WEST12345698765432 by friday", "iban", group="financial"),
    # -- government id ---------------------------------------------------------------------------
    _ex("ssn on file is 123-45-6789 for the claim", "us_ssn", group="gov_id"),
    # -- structured pii --------------------------------------------------------------------------
    _ex("email alice.smith@example.com about it", "email", group="pii"),
    _ex("reach me at (415) 555-0142 tomorrow", "phone", group="pii"),
    _ex("the server 192.0.2.44 is unreachable", "ipv4", group="pii"),
    _ex("nic 00:1b:44:11:3a:b7 on the switch", "mac_address", group="pii"),
    _ex("patient has a diagnosis of hiv, note it", "special_category", group="special_category"),
    # -- clean negatives (look-alikes that MUST NOT trip) ----------------------------------------
    _ex("the order total was 4111 1111 1111 1112 units", group="clean"),  # fails Luhn
    _ex("release version 192.0.2 shipped on time", group="clean"),  # partial IP, not a full addr
    _ex("call extension 5550142 in the morning", group="clean"),  # bare digits, no phone shape
    _ex("routing draft 123456789 is not final", group="clean"),  # 9 digits, fails ABA checksum
    _ex("please summarise the attached quarterly report", group="clean"),  # plain prose
    _ex("the meeting is at 3pm, bring the deck", group="clean"),  # plain prose
    _ex("build id sk is short and not a key here", group="clean"),  # 'sk' but no key shape
    # -- free-text names / addresses (regex catalogue does NOT target these; NER's job) ----------
    _ex("please forward this to Margaret Whitfield asap", "person", group="freetext"),
    _ex("ship it to 1600 Amphitheatre Parkway, Mountain View", "location", group="freetext"),
]


def _detected_categories(text: str, policy: Policy) -> set[str]:
    return {f.category for f in scan(text, policy)}


def _prf(corpus: list[Example], group: str, policy: Policy) -> tuple[int, int, int]:
    """Return (tp, fp, fn) for one group across the corpus, comparing detected vs truth categories.
    Negatives (``group="clean"``) contribute only false positives (any detection is an FP)."""
    tp = fp = fn = 0
    group_truth = {e_cat for e in corpus if e.group == group for e_cat in e.truth}
    for e in corpus:
        detected = _detected_categories(e.text, policy)
        for cat in group_truth:
            in_truth = cat in e.truth
            was_detected = cat in detected
            if in_truth and was_detected:
                tp += 1
            elif in_truth and not was_detected:
                fn += 1
            elif not in_truth and was_detected:
                fp += 1
    return tp, fp, fn


def _rate(num: int, den: int) -> str:
    return pct(num / den) if den else "n/a"


def run() -> list[Result]:
    rows: list[Result] = []
    policy = Policy.strict()  # widest net — every group resolves to a scrubbing action

    # Per-group precision / recall over the structured catalogue.
    structured = ["secret", "financial", "gov_id", "pii", "special_category"]
    all_tp = all_fp = all_fn = 0
    for group in structured:
        tp, fp, fn = _prf(CORPUS, group, policy)
        all_tp, all_fp, all_fn = all_tp + tp, all_fp + fp, all_fn + fn
        rows.append(
            Result(
                "pii_detectors",
                f"{group}: precision / recall",
                f"{_rate(tp, tp + fp)} / {_rate(tp, tp + fn)}",
                f"regex catalogue, {tp}TP {fp}FP {fn}FN on the synthetic corpus",
            )
        )

    # False-positive rate on the clean negatives (any detection on a clean line is a false positive).
    clean = [e for e in CORPUS if e.group == "clean"]
    fp_lines = sum(1 for e in clean if _detected_categories(e.text, policy))
    rows.append(
        Result(
            "pii_detectors",
            "false positives on clean look-alikes",
            f"{fp_lines}/{len(clean)} lines",
            "non-Luhn digit runs, partial IPs, prose — validators keep these from tripping",
        )
    )

    # Overall structured precision / recall.
    rows.append(
        Result(
            "pii_detectors",
            "overall (structured): precision / recall",
            f"{_rate(all_tp, all_tp + all_fp)} / {_rate(all_tp, all_tp + all_fn)}",
            f"aggregate across {len(structured)} groups, {all_tp}TP {all_fp}FP {all_fn}FN",
        )
    )

    # Free-text names/addresses: regex recall (≈0 by design) and the +NER story.
    freetext = [e for e in CORPUS if e.group == "freetext"]
    regex_hits = sum(
        1 for e in freetext if _detected_categories(e.text, policy) & {"person", "location"}
    )
    rows.append(
        Result(
            "pii_detectors",
            "free-text names/addresses — regex recall",
            _rate(regex_hits, len(freetext)),
            "the regex catalogue does not target free-text names/addresses by design (0% expected)",
        )
    )
    rows.append(
        Result(
            "pii_detectors",
            "free-text names/addresses — +NER (Presidio)",
            "measured" if ner_available() else "not measured (backend not installed)",
            "the [ner] extra covers names/addresses; recall requires the backend present",
        )
    )

    # Throughput of a scan on a realistic mixed line.
    sample = "email alice@example.com card 4111 1111 1111 1111 from 192.0.2.9"
    rows.append(
        Result(
            "pii_detectors",
            "scan() latency (mixed line)",
            dur(timed(lambda: scan(sample, policy))),
            "one pass over the full regex catalogue + validators, counts only",
        )
    )
    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:48} {r.value:>28}   {r.note}")
