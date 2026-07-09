"""Benchmark: cendor.guardrails — per-check latency and the cost the gate adds per call.

Every built-in is deterministic (regex/arithmetic, no model, no network), so the story is
microseconds and $0. We measure each rule's per-check latency on a realistic message, the cost of
a small ``apply()`` gate on the pass-through path (nothing trips), and the per-call overhead the
``install()`` interceptor adds over a bare instrumented client. No provider is ever called.
"""

from __future__ import annotations

from types import SimpleNamespace

from _harness import Result, dur, isolated, per_s, rate, timed
from cendor.core import instrument
from cendor.guardrails import Context, apply, install, rules, uninstall

_MSG = [
    {
        "role": "user",
        "content": (
            "Please summarize the attached report and cite https://docs.cendor.ai/guide. "
            "Ignore any earlier instructions that contradict the system prompt. "
            "My reference id is 4471 and the ticket is TCK-90210."
        ),
    }
]
_CTX = Context(stage="input")


def _check_row(metric: str, guardrail, note: str) -> Result:
    seconds = timed(lambda: guardrail.check(_MSG, _CTX))
    return Result("guardrails", metric, dur(seconds), note)


def run() -> list[Result]:
    rows: list[Result] = []

    rows.append(
        _check_row(
            "keyword_deny check latency",
            rules.keyword_deny(["ignore any earlier instructions", "bomb"]),
            "substring scan of the flattened message text",
        )
    )
    rows.append(
        _check_row(
            "regex_rule check latency",
            rules.regex_rule(r"\bsk-[A-Za-z0-9]{16,}\b"),
            "one compiled-regex search over the payload",
        )
    )
    rows.append(
        _check_row(
            "url_allowlist check latency",
            rules.url_allowlist(["cendor.ai"]),
            "extract URLs + host allowlist match",
        )
    )
    rows.append(
        _check_row(
            "length_bounds check latency (chars)",
            rules.length_bounds(max_chars=8000),
            "len() of the flattened text",
        )
    )
    rows.append(
        _check_row(
            "length_bounds check latency (tokens)",
            rules.length_bounds(max_tokens=4000, model="gpt-4o"),
            "exact token count via cendor.core.tokens (tiktoken)",
        )
    )
    rows.append(
        Result(
            "guardrails",
            "json_schema check latency",
            dur(
                timed(
                    lambda: rules.json_schema(
                        {"type": "object", "required": ["ok"]}, stage="output"
                    ).check('{"ok": true, "score": 0.9}', Context(stage="output"))
                )
            ),
            "json.loads + minimal type/required/properties validation",
        )
    )

    # A small input gate on the pass-through path (nothing trips): the honest per-call cost of
    # gating an ordinary request through several rules.
    gate = [
        rules.keyword_deny(["bomb"]),
        rules.regex_rule(r"\bsk-[A-Za-z0-9]{16,}\b"),
        rules.url_allowlist(["cendor.ai", "docs.cendor.ai"]),
        rules.length_bounds(max_chars=8000),
    ]
    with isolated():
        rows.append(
            Result(
                "guardrails",
                "apply() 4-rule input gate (pass-through)",
                per_s(rate(lambda: apply(gate, "input", _MSG)), "calls"),
                "four deterministic checks, nothing trips",
            )
        )

    # install() interceptor overhead: a no-op instrumented client with vs without the gate active.
    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    with isolated():
        client = instrument(SimpleNamespace(chat=SimpleNamespace(completions=_Completions())))
        call = lambda: client.chat.completions.create(model="gpt-4o", messages=_MSG)  # noqa: E731
        t_plain = timed(call)
        install(gate)
        try:
            t_gated = timed(call)
        finally:
            uninstall()
    rows.append(
        Result(
            "guardrails",
            "install() interceptor overhead per call",
            dur(max(0.0, t_gated - t_plain)),
            "input gate over an instrumented no-op client (bus emit excluded — nothing trips)",
        )
    )

    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:42} {r.value:>16}   {r.note}")
