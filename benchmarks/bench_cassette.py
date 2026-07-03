"""Benchmark: cendor.cassette — replay overhead, record/replay speedup, semantic match.

The headline ("agent tests in 0.2s, no API key") comes from skipping the real call on replay. We
model a real call with a fake client that sleeps a few ms (real LLM calls are 100×–1000× slower),
then measure: a full N-call run live vs replayed, the per-call overhead cassette itself adds, and
that ``semantic_match`` accepts a paraphrase while rejecting a mismatch.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from _harness import Result, dur, isolated, timed
from cendor import cassette
from cendor.core import instrument

_LIVE_MS = 4.0  # stand-in for network + model latency; real calls are far slower
_N = 25


def _make_client(sleep_s: float) -> SimpleNamespace:
    """An OpenAI-shaped fake client; ``create`` sleeps to model latency and returns usage."""

    def create(*, model: str, messages: list, **kw: object) -> SimpleNamespace:
        if sleep_s:
            time.sleep(sleep_s)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="A refund will be issued shortly."))
            ],
            usage=SimpleNamespace(
                prompt_tokens=900, completion_tokens=60, prompt_tokens_details=None
            ),
        )

    completions = SimpleNamespace(create=create)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _agent(client: SimpleNamespace, prompts: list[str]) -> list[str]:
    out = []
    for p in prompts:
        r = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": p}]
        )
        out.append(r.choices[0].message.content)
    return out


def run() -> list[Result]:
    rows: list[Result] = []
    prompts = [f"Q{i}: why was invoice {1000 + i} charged twice?" for i in range(_N)]

    with isolated(), tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "agent.json")
        client = instrument(_make_client(_LIVE_MS / 1000.0))

        # Record once (runs live, writes the cassette).
        cassette.use(path, mode="record")(lambda: _agent(client, prompts))()

        live = timed(lambda: _agent(client, prompts), target_seconds=0.2, warmup=1)
        replay_run = cassette.use(path, mode="replay")(lambda: _agent(client, prompts))
        replayed = timed(replay_run, target_seconds=0.2, warmup=2)

        rows.append(
            Result(
                "cassette",
                f"{_N}-call run: replayed vs live",
                f"{dur(replayed)} vs {dur(live)}",
                f"live = fake client sleeping {_LIVE_MS:.0f} ms/call (real LLMs are far slower)",
            )
        )
        rows.append(
            Result(
                "cassette",
                "Replay speedup",
                f"{live / replayed:.0f}×",
                f"at the modeled {_LIVE_MS:.0f} ms/call; scales with real latency",
            )
        )
        rows.append(
            Result(
                "cassette",
                "Replay overhead per call",
                dur(replayed / _N),
                "hash the request, look up the recorded response, reconstruct it",
            )
        )

        # semantic_match: meaning, not bytes.
        accepts = cassette.semantic_match(
            "The agent offers the customer a full refund.", "offers a refund"
        )
        rejects = not cassette.semantic_match("Your account is now locked.", "offers a refund")
        rows.append(
            Result(
                "cassette",
                "semantic_match (lexical default)",
                "✓ accept + reject" if (accepts and rejects) else "FAILED",
                "accepts a paraphrase, rejects an unrelated answer",
            )
        )

    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:42} {r.value:>20}   {r.note}")
