"""Benchmark: cendor.core token counting — accuracy vs tiktoken + throughput.

Effectiveness: how close the *offline* heuristic lands to a real tokenizer. For the OpenAI family
the ground truth is tiktoken itself (so the ``[tiktoken]`` extra is exact, 0% error); the heuristic
is what you get with zero extra deps. For Claude/Gemini there is no offline native tokenizer, so we
report the structure-aware estimate against tiktoken's o200k as a *cross-tokenizer reference* — a
ballpark sanity check, clearly not the native tokenizer.

Speed: ``tokens.count`` calls/sec for each path.
"""

from __future__ import annotations

from _data import prose_doc
from _harness import Result, isolated, pct, per_s, rate, tiktoken_present
from cendor.core import tokens

# A non-repeating accuracy corpus. The compression generators in _data repeat their unit to reach a
# size, which BPE tokenizes unusually efficiently and would unfairly inflate the heuristic's error;
# natural, varied text is the fair test of "how close is chars/N to a real tokenizer?".
_PROSE = (
    "Large language models read context as a sequence of tokens, and every token competes for a "
    "finite budget. Engineers often discover too late that a verbose system prompt has crowded out "
    "the retrieved evidence the model actually needed. Good context hygiene treats the window as a "
    "scarce resource: pin what must survive, compress what can be summarized, and drop what merely "
    "pads the request. Pricing follows tokens directly, so trimming waste lowers both latency and "
    "cost. The hardest part is measurement, because token counts vary by tokenizer and by model "
    "family, and a reliable estimate lets a team reason about budgets before a single call is made."
)
_CODE = '''\
def reconcile(invoices, payments):
    """Match payments to invoices and return the unreconciled balance per account."""
    balance = {}
    for inv in invoices:
        balance[inv.account] = balance.get(inv.account, 0) + inv.amount
    for pay in payments:
        if pay.account in balance:
            balance[pay.account] -= pay.amount
    return {acct: amt for acct, amt in balance.items() if amt != 0}
'''
_JSON = (
    '{"id":"acct_4471","plan":"enterprise","seats":42,"region":"eu-west","mrr":12990.50,'
    '"owner":{"name":"Dana Okoro","email":"dana@example.com","verified":true},'
    '"features":["sso","audit-log","priority-support"],"trial_ends":null,"created":"2025-11-03"}'
)


def _corpus() -> dict[str, str]:
    return {"prose": _PROSE, "code": _CODE, "json": _JSON}


def _heuristic_count(text: str, model: str) -> int:
    """Force core's offline heuristic even when tiktoken is installed (patch out both encoders)."""
    saved_tt, saved_o2 = tokens._tiktoken_encoding, tokens._o200k
    tokens._tiktoken_encoding = lambda _model: None  # type: ignore[assignment]
    tokens._o200k = lambda: None  # type: ignore[assignment]
    try:
        return tokens.count(text, model)
    finally:
        tokens._tiktoken_encoding = saved_tt  # type: ignore[assignment]
        tokens._o200k = saved_o2  # type: ignore[assignment]


def run() -> list[Result]:
    rows: list[Result] = []
    corpus = _corpus()

    with isolated():
        if tiktoken_present():
            import tiktoken

            enc = tiktoken.get_encoding("o200k_base")

            # OpenAI family, per content kind: the offline heuristic (chars/4) vs the real tokenizer.
            # Prose tracks well; code/JSON are token-denser, so the heuristic drifts — which is the
            # honest signal to install the [tiktoken] extra for code-heavy prompts.
            for kind, text in corpus.items():
                truth = len(enc.encode(text))
                est = _heuristic_count(text, "gpt-4o")
                rows.append(
                    Result(
                        "core",
                        f"Offline heuristic error vs tiktoken — {kind}",
                        pct(abs(est - truth) / truth, 1),
                        f"heuristic {est} vs exact {truth} tokens",
                    )
                )

            # Exact mode: tiktoken is a required dependency, so core IS tiktoken for OpenAI -> 0%.
            exact_ok = all(tokens.count(t, "gpt-4o") == len(enc.encode(t)) for t in corpus.values())
            rows.append(
                Result(
                    "core",
                    "Exact mode error (default)",
                    pct(0.0, 1) if exact_ok else "mismatch",
                    "OpenAI counts are exact out of the box — `tiktoken` is a required dependency",
                )
            )

            # Claude/Gemini: structure-aware estimate vs a cross-tokenizer reference (o200k).
            cerrs = [
                abs(_heuristic_count(t, "claude-opus-4-8") - len(enc.encode(t)))
                / len(enc.encode(t))
                for t in corpus.values()
            ]
            rows.append(
                Result(
                    "core",
                    "Offline subword fallback vs o200k (Claude/Gemini)",
                    pct(sum(cerrs) / len(cerrs), 1),
                    "the defensive no-tiktoken fallback; by default Claude/Gemini use o200k directly",
                )
            )
            rows.append(
                Result(
                    "core",
                    "Counting path (default)",
                    f"OpenAI={tokens.method('gpt-4o')}, Claude={tokens.method('claude-opus-4-8')}",
                    "method() picks exact / bpe-estimate automatically; heuristic only if tiktoken "
                    "fails to import",
                )
            )
        else:
            rows.append(
                Result(
                    "core",
                    "Token accuracy vs tiktoken",
                    "skipped",
                    "install the [tiktoken] extra to measure heuristic error / exact mode",
                )
            )

        # Throughput. Measure each path on a fixed prose string; note its size.
        text = prose_doc(3)
        kb = len(text.encode("utf-8")) / 1024
        rows.append(
            Result(
                "core",
                "tokens.count throughput — OpenAI heuristic",
                per_s(rate(lambda: _heuristic_count(text, "gpt-4o"))),
                f"on a {kb:.1f} KB string",
            )
        )
        rows.append(
            Result(
                "core",
                "tokens.count throughput — subword estimate",
                per_s(rate(lambda: _heuristic_count(text, "claude-opus-4-8"))),
                f"on a {kb:.1f} KB string",
            )
        )
        if tiktoken_present():
            rows.append(
                Result(
                    "core",
                    "tokens.count throughput — tiktoken exact",
                    per_s(rate(lambda: tokens.count(text, "gpt-4o"))),
                    f"on a {kb:.1f} KB string",
                )
            )

    return rows


if __name__ == "__main__":
    for r in run():
        print(f"{r.metric:55} {r.value:>12}   {r.note}")
