# cendor-guardrails

A local-first **gate** for LLM apps: define a check — keyword, regex, URL, length, JSON-schema —
attach it to a stage (`input`, `tool_call`, `tool_output`, `output`), and block, redact, or flag
before the model or a tool ever runs. No server, no account, no model call.

**Deterministic checks in microseconds for $0 — and every decision lands in a tamper-evident audit chain.**

![PyPI](https://img.shields.io/pypi/v/cendor-guardrails) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-guardrails`

```python
from cendor.core import instrument
from cendor.guardrails import install, rules

client = instrument(OpenAI())
install([                                           # one interceptor gates every call
    rules.keyword_deny(["ignore previous instructions"], action="block"),
    rules.regex_rule(r"\bsk-[A-Za-z0-9]{20,}\b", action="redact", stage="input"),
    rules.url_allowlist(["docs.cendor.ai"], stage="input"),
])

client.chat.completions.create(model="gpt-4o", messages=msgs)
# blocked prompt -> raises GuardrailTripped BEFORE the request is sent ($0 spent)
# a leaked key -> the provider receives "[redacted]" instead
```

## Highlights

- **Four intervention points** — gate the user turn (`input`), the model's request to call a tool
  (`tool_call`), the tool's result (`tool_output`), and the model's final answer (`output`).
  Matches Azure Foundry's intervention points and OpenAI's four decorator types.
- **Deterministic built-ins, no heavy deps** — `keyword_deny`, `regex_rule`, `url_allowlist` /
  `url_deny`, `length_bounds` (char + **exact** token bounds via `cendor.core.tokens`),
  `json_schema`, and `custom`. Regex/arithmetic only — offline, deterministic, $0.
- **Evidence, not just enforcement** — every trip or flag emits a `GuardrailDecision` on the
  `cendor.core` bus, so `cendor-acttrace` chains it as a tamper-evident `guardrail_decision` entry
  with **no import** between the two. "We blocked it" is in the hash chain, not a log line.
- **Three ways to use it** — pure `apply()` / `evaluate()`; framework-independent `install()` on
  the core seam (or **`scoped()`** for per-request gating on a concurrent server); and
  `Agent(guardrails=[…])` in `cendor-sdk` (all four in-loop stages + per-run override).
- **Bring-your-own model judge** — `rules.llm_judge` for open-ended risk, with per-guardrail
  `timeout` + `on_error` (fail-closed by default) and `cendor.guardrails.judge` helpers (verdict
  prompt + strict-JSON parsing). The judge rides an instrumented client, so its own spend is
  budgeted + audited.

```python
from cendor.guardrails import apply, guardrail, Verdict, GuardrailTripped

@guardrail(stage="output")
def must_be_json(payload, ctx):
    if not payload.strip().startswith("{"):
        return Verdict("block", reason="expected a JSON object")

try:
    apply([must_be_json], "output", model_text)      # raises GuardrailTripped on a block
except GuardrailTripped as e:
    print(e.decisions)                                 # the recorded decisions, block last
```

## How it plugs into your agent

`guardrails` is the **Gate** in the pipeline — `contextkit → squeeze → tokenguard → guardrails →
cassette → acttrace`. It imports **only** `cendor-core`: checks ride the same `instrument()` seam
and event bus every other library uses, so the same guardrail works under `cendor-sdk`, a bare
instrumented client, or beneath another framework — in Python and TypeScript alike.

**Honest limits:** the built-ins are deterministic, so they do not stop a *novel* adversarial
attack — a jailbreak they were never told about will pass. Pair them with a bring-your-own model
judge (`rules.llm_judge`, an adapter contract — you supply the call, and the extra latency/cost is
real) and treat the deterministic rules as the fast, free floor, not a ceiling. PII/secret
detection lives in `cendor-acttrace` (`guard(Policy…)`), not here.

*Part of the Cendor stack — github.com/cendorhq/cendor-libs. Powered by PowerAI Labs.*
