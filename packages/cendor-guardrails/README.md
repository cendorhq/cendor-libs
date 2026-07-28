# cendor-guardrails

A local-first **gate** for LLM apps: define a check — keyword, regex, URL, length, JSON-schema —
attach it to a stage (`input`, `tool_call`, `tool_output`, `output`), and block, redact, or flag
before the model or a tool ever runs. No server, no account, no model call.

**Deterministic checks in microseconds for $0 — and every decision lands in a tamper-evident audit chain.**

![PyPI](https://img.shields.io/pypi/v/cendor-guardrails) ![license](https://img.shields.io/badge/license-Apache_2.0-blue) · `pip install cendor-guardrails`

Using an AI coding assistant? `npx @cendor/init` (TS) / `uvx cendor-init` (Python) wires it up — or point it at [cendor.ai/docs/for-ai-assistants](https://cendor.ai/docs/for-ai-assistants).

```python
from cendor.core import instrument
from cendor.guardrails import install, rules

client = instrument(OpenAI())
install([                                           # one interceptor gates every call
    rules.keyword_deny(["ignore previous instructions"], action="block"),
    rules.regex_rule(r"\bsk-[A-Za-z0-9]{20,}\b", action="redact", stage="input"),
    rules.url_allowlist(["cendor.ai"], stage="input"),
])

client.chat.completions.create(model="gpt-4o", messages=msgs)
# blocked prompt -> raises GuardrailTripped BEFORE the request is sent ($0 spent)
# a leaked key -> the provider receives "[redacted]" instead
```

## Highlights

- **Four intervention points** — gate the user turn (`input`), the model's request to call a tool
  (`tool_call`), the tool's result (`tool_output`), and the model's final answer (`output`).
  Matches Azure Foundry's intervention points and OpenAI's four decorator types.
- **Deterministic built-ins, no heavy deps** — `keyword_deny`, `regex_rule`, `spotlight` (wrap
  untrusted content in a trust-lowering delimiter — a `$0` mitigation, inspired by Azure
  Spotlighting), `url_allowlist` / `url_deny`, `length_bounds` (char + **exact** token bounds via
  `cendor.core.tokens`), `json_schema`, and `custom`. Regex/arithmetic only — offline, deterministic, $0.
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
- **Detection tiers you opt into** — a local classifier contract (`rules.classifier`,
  `rules.prompt_guard` behind the `[promptguard]` extra), `rules.language`, and hosted rails
  (`rules.bedrock_guardrail` / `azure_content_safety` / `model_armor` — duck-typed clients, metered
  by the vendor). Every hosted verdict still emits a **local** `guardrail_decision`: cloud check,
  local evidence. No jailbreak/PII-catch-rate claim ships without a reproduced, published benchmark.
- **Config as data + grounding** — `load_policy("guardrails.yaml")` builds deterministic rules from
  a versioned file and stamps its `policy_hash` / `policy_version` onto every decision (the audit
  chain proves which policy was live); `rules.groundedness` / `rules.denied_topics` gate on
  bring-your-own-embedding cosine similarity (RAG hallucination / off-topic), no bundled model.
- **Red-team it** — `run_redteam(guardrails, load_corpus("attacks.jsonl"))` reports the trip rate +
  false-positive rate against a labeled corpus **you** supply (cendor vends no attack data). A
  measurement, not a claim: publish a rate only with the corpus named.

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

See [`docs/guardrails.md`](https://github.com/cendorhq/cendor-libs/blob/main/docs/guardrails.md) · [CHANGELOG](https://github.com/cendorhq/cendor-libs/blob/main/packages/cendor-guardrails/CHANGELOG.md). *Part of the Cendor stack — [github.com/cendorhq/cendor-libs](https://github.com/cendorhq/cendor-libs). Powered by PowerAI Labs. Apache-2.0; provided "as is", without warranty — use at your own risk (LICENSE §7–8).*
