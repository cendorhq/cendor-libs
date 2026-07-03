"""Deterministic sample data for the benchmarks — fixed seeds, no external fixtures.

Each generator returns a string shaped like real context that flows into an LLM call: a verbose
JSON API response, noisy application logs, a source file, and prose. Sizes are kept in the tens of
KB so compression ratios and MB/s are meaningful but the suite stays fast. Nothing here is rigged
to flatter the compressors — the input characteristics are described in docs/benchmarks.md.
"""

from __future__ import annotations

import json
import random

_CITIES = ["Berlin", "Lagos", "Quito", "Osaka", "Toronto", "Cairo", "Oslo", "Lima", "Pune", "Accra"]
_TAGS = ["beta", "vip", "trial", "churned", "enterprise", "free", "internal", "partner"]


def verbose_json(n_records: int = 220) -> str:
    """A pretty-printed JSON array with null fields, as many REST APIs and ``json.dumps(indent=2)``
    dumps actually produce. squeeze's JSON path minifies whitespace and drops null keys (the original
    stays restorable), so the ratio reflects formatting + null overhead, not data loss.
    """
    rng = random.Random(11)
    records = []
    for i in range(n_records):
        records.append(
            {
                "id": i,
                "uuid": f"{rng.getrandbits(128):032x}",
                "name": f"user_{i}",
                "email": f"user{i}@example.com",
                "city": rng.choice(_CITIES),
                "middle_name": None,
                "phone": None if rng.random() < 0.6 else f"+1-555-{rng.randint(1000, 9999)}",
                "verified": rng.random() < 0.5,
                "deleted_at": None,
                "score": round(rng.random() * 100, 4),
                "address": {
                    "line2": None,
                    "country": rng.choice(["US", "DE", "NG", "JP", "CA"]),
                    "postal": None if rng.random() < 0.5 else f"{rng.randint(10000, 99999)}",
                },
                "tags": rng.sample(_TAGS, k=rng.randint(0, 3)),
                "notes": None,
            }
        )
    return json.dumps(records, indent=2)


def noisy_logs(n_lines: int = 1200) -> str:
    """Application logs: timestamped, leveled, UUID-bearing, with the repetition real logs have.

    squeeze normalizes volatile fields (timestamps -> <ts>, UUIDs -> <uuid>) then dedupes identical
    lines into ``(×N)`` — so health-check / heartbeat spam collapses hard, as it does in production.
    """
    rng = random.Random(7)
    out = []
    for _ in range(n_lines):
        ts = f"2026-06-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z"
        roll = rng.random()
        if roll < 0.55:  # heartbeat / health spam — the bulk of most logs
            out.append(f"{ts} INFO  GET /health 200 1ms")
        elif roll < 0.75:
            uid = f"{rng.getrandbits(128):032x}"
            uid = f"{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:32]}"
            out.append(f"{ts} INFO  request {uid} POST /api/v1/jobs 201 42ms")
        elif roll < 0.92:
            out.append(f"{ts} INFO  cache hit key=session:{rng.randint(1, 50)}")
        else:
            out.append(f"{ts} ERROR upstream timeout after 30000ms (attempt {rng.randint(1, 3)})")
    return "\n".join(out)


def noisy_logs_mixed(n_lines: int = 1200) -> str:
    """A higher-entropy log: only ~15% identical heartbeat spam; most lines carry a genuinely unique
    free-text slug (a short non-hex token normalization does *not* blank out), so they can't dedup.
    This is the honest *lower bound* on log compression — reported alongside the repetition-heavy
    ``noisy_logs`` so the headline ratio isn't read as typical of every log."""
    rng = random.Random(11)
    paths = ["/api/v1/jobs", "/api/v1/users", "/api/v1/orders", "/search", "/upload", "/report"]
    verbs = ["GET", "POST", "PUT", "DELETE"]
    errors = ["upstream timeout", "db deadlock", "oom killed", "disk full", "cert expired"]

    # 6 chars from g-z only: never all-hex, never a bare integer -> normalization won't blank it.
    def slug() -> str:
        return "".join(rng.choice("ghijklmnopqrstuvwxyz") for _ in range(6))

    out = []
    for _ in range(n_lines):
        ts = f"2026-06-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z"
        roll = rng.random()
        if roll < 0.15:  # a little heartbeat spam, not the bulk
            out.append(f"{ts} INFO  GET /health 200 1ms")
        elif roll < 0.60:
            out.append(
                f"{ts} INFO  {rng.choice(verbs)} {rng.choice(paths)} "
                f"{rng.choice([200, 201, 204, 404])} {rng.randint(2, 900)} ms tenant-{slug()} op-{slug()}"
            )
        elif roll < 0.85:
            out.append(
                f"{ts} WARN  slow query {rng.randint(100, 5000)} ms plan-{slug()} on {slug()}"
            )
        else:
            out.append(
                f"{ts} ERROR {rng.choice(errors)} on {slug()}-{slug()} (attempt {rng.randint(1, 5)})"
            )
    return "\n".join(out)


def code_sample(repeat: int = 14) -> str:
    """A source file with comments and blank lines — squeeze's code path strips those, keeps logic."""
    unit = '''\
# ---------------------------------------------------------------------------
# Module: payments helper. Auto-generated header, safe to strip.
# ---------------------------------------------------------------------------

import math               # stdlib only
from decimal import Decimal


def settle(amount, rate):
    # Convert to Decimal first to avoid float noise in money math.
    base = Decimal(str(amount))

    fee = base * Decimal(str(rate))      # provider fee

    # Round half-up to cents.
    return (base + fee).quantize(Decimal("0.01"))


class Ledger:
    """A tiny in-memory ledger. The docstring and blank lines below are noise."""

    def __init__(self):
        self.entries = []          # list of (id, amount)


    def add(self, entry_id, amount):
        # Append; no validation here on purpose.
        self.entries.append((entry_id, amount))

'''
    return "\n".join(unit for _ in range(repeat))


def prose_doc(repeat: int = 18) -> str:
    """English prose — squeeze's extractive path keeps the highest keyword-density sentences."""
    para = (
        "The context window behaves like a packed suitcase rather than an infinite buffer. "
        "Every token you spend on boilerplate is a token unavailable for the actual task. "
        "Teams routinely paste entire documents into a prompt and wonder why quality drops. "
        "Compression that preserves meaning lets the model attend to what matters. "
        "Reversibility is essential because the full source is sometimes needed downstream. "
        "A receipt of what was kept and dropped turns prompt assembly into an auditable step. "
    )
    return " ".join(para for _ in range(repeat))


def chat_messages(n_turns: int = 8) -> list[dict]:
    """A small multi-turn chat, for token-counting benchmarks over message lists."""
    msgs = [
        {"role": "system", "content": "You are a precise, terse assistant for billing support."}
    ]
    for i in range(n_turns):
        msgs.append(
            {"role": "user", "content": f"Why was I charged ${i + 1}.99 on invoice {1000 + i}?"}
        )
        msgs.append(
            {
                "role": "assistant",
                "content": f"Invoice {1000 + i} includes a prorated plan change; the ${i + 1}.99 line is the proration.",
            }
        )
    return msgs
