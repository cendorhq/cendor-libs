"""Benchmark: instrument() / bus overhead and tokenguard's added cost per call.

The whole stack rides one ``instrument()`` seam, so the question that matters is: how cheap is it to
add? We measure the overhead instrument() adds to a call (bus emit + usage extraction + pricing),
the extra tokenguard's subscriber adds on top (spend record + budget check), how fast ``report()``
aggregates many spend rows, and raw bus dispatch throughput.
"""

from __future__ import annotations

from types import SimpleNamespace

from _harness import Result, dur, isolated, per_s, rate, timed
from cendor.core import bus, instrument
from cendor.tokenguard import budget, report, reset, track

_MSGS = [{"role": "user", "content": "Summarize invoice 1042 and explain the duplicate charge."}]


def _make_client() -> SimpleNamespace:
    def create(*, model: str, messages: list, **kw: object) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(
                prompt_tokens=900, completion_tokens=60, prompt_tokens_details=None
            ),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _call(client: SimpleNamespace) -> object:
    return client.chat.completions.create(model="gpt-4o", messages=_MSGS)


def run() -> list[Result]:
    rows: list[Result] = []

    # 1) instrument() overhead = instrumented call − raw call (no subscribers).
    with isolated():
        raw = _make_client().chat.completions.create
        t_raw = timed(lambda: raw(model="gpt-4o", messages=_MSGS))
        inst = _make_client()
        instrument(inst)
        t_inst = timed(lambda: _call(inst))
        rows.append(
            Result(
                "core",
                "instrument() overhead per call",
                dur(max(0.0, t_inst - t_raw)),
                "bus emit + usage extraction + Decimal pricing; over a no-op client",
            )
        )

    # 2) tokenguard's added cost = call with @budget+track active − bare instrumented call.
    with isolated():
        bare = _make_client()
        instrument(bare)
        t_bare = timed(lambda: _call(bare))
    with isolated():
        reset()  # arms tokenguard's bus subscriber + downgrade interceptor
        guarded = _make_client()
        instrument(guarded)
        with budget(usd=1e9), track(feature="support", user_id="alice"):
            t_guard = timed(lambda: _call(guarded))
        reset()
    rows.append(
        Result(
            "tokenguard",
            "Added overhead per call (@budget + track)",
            dur(max(0.0, t_guard - t_bare)),
            "records spend by tags + checks the active budget(s)",
        )
    )

    # 3) report() aggregation over many spend rows.
    with isolated():
        reset()
        gen = _make_client()
        instrument(gen)
        m = 5000
        for i in range(m):
            with track(feature=f"feat{i % 5}", user_id=f"u{i % 100}"):
                _call(gen)
        spc = timed(lambda: report(group_by=["feature", "user_id"]))
        rows.append(
            Result(
                "tokenguard",
                f"report() over {m} spend rows",
                dur(spc),
                "group-by aggregation into per-tag cost rows",
            )
        )
        reset()

    # 4) raw bus dispatch with a realistic subscriber count (tokenguard + cassette + acttrace ≈ 3).
    with isolated():
        for i in range(3):
            bus.subscribe(lambda _e, _i=i: None)
        rows.append(
            Result(
                "core",
                "bus dispatch (3 subscribers)",
                per_s(rate(lambda: bus.emit(object())), "emits"),
                "synchronous fan-out to subscribed tools",
            )
        )

    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:48} {r.value:>14}   {r.note}")
