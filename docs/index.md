# Cendor

**Production plumbing for LLM applications.**

Composable Python primitives for context, cost, testing, and governance — the layer beneath your
LLM app. Framework-agnostic · local-first · offline by default · Apache-2.0.

## The problem

You shipped an LLM agent. Then production happened:

- 🧠 **Prompts overflow the context window** — and naive truncation drops exactly the wrong things.
- 💸 **Cost is a black box** — a looping agent quietly burns money, and you can't say which feature or user spent it.
- 🧪 **You can't test it** — every run hits a paid, non-deterministic API, so there are no fast, repeatable tests.
- 📋 **There's no audit trail** — you can't show what the agent saw, did, cost, or *refused to do*.

Agent frameworks (LangChain, LlamaIndex, the provider SDKs) decide *what* your agent does. They
don't handle these cross-cutting, *under-the-call* concerns. **Cendor does — and you keep your
framework.**

## The fix: wrap your client once, every tool plugs in

```python
from cendor.core import instrument
client = instrument(OpenAI())   # ← the one line you change
```

That single wrap publishes every LLM and tool call onto an in-process **event bus**. Each library
*subscribes* — none patches your client, none imports another — so you add budgeting, recording, or
auditing with **zero per-call wiring**. Read the diagram top to bottom: it's one request's lifecycle,
with each library labelled **where it acts**.

```mermaid
%%{init: {"flowchart": {"htmlLabels": false}} }%%
graph TD
    YOU["your agent code"]
    B["1. Build the prompt"]
    PRE["2. Pre-flight<br/>(before the call runs)"]
    CALL["3. The LLM call<br/>core.instrument() = the seam"]
    POST["4. After the call<br/>(automatic, via the event bus)"]

    YOU --> B --> PRE --> CALL --> POST

    B --- CK["contextkit<br/>pack context into a budget"]
    B --- SQ["squeeze<br/>compress oversized blocks"]
    PRE --- TG1["tokenguard<br/>block / downgrade if over budget"]
    PRE --- AT1["acttrace<br/>policy guard: flag + block bad input"]
    POST --- TG2["tokenguard<br/>record spend by feature / user"]
    POST --- CS["cassette<br/>record the run (replay in tests)"]
    POST --- AT2["acttrace<br/>append to the tamper-evident log"]

    classDef seam fill:#2563EB,color:#ffffff,stroke:#1E40AF;
    classDef ck fill:#3B82F6,color:#ffffff,stroke:#2563EB;
    classDef sq fill:#22C55E,color:#0F172A,stroke:#16A34A;
    classDef tg fill:#8B5CF6,color:#ffffff,stroke:#7C3AED;
    classDef cs fill:#14B8A6,color:#ffffff,stroke:#0D9488;
    classDef at fill:#F43F5E,color:#ffffff,stroke:#E11D48;
    class CALL seam;
    class CK ck;
    class SQ sq;
    class TG1,TG2 tg;
    class CS cs;
    class AT1,AT2 at;
```

**tokenguard** and **acttrace** each appear twice: they run *before* the call (cap spend / guard
the input) **and** *after* it (record cost / append to the log).

## The six libraries

Each solves one of those problems, and each works on its own:

| Library | Solves | In one line |
|---|---|---|
| [contextkit](contextkit.md) | prompts overflow | Pack prioritized blocks into a token budget; get a receipt of what was kept / shrunk / dropped. |
| [squeeze](squeeze.md) | a blob is too big | Content-aware, deterministic compression (JSON / logs / code / prose) — fully reversible. |
| [tokenguard](tokenguard.md) | runaway cost | Cap spend before a call runs (block / downgrade), and attribute cost per feature / user. |
| [cassette](cassette.md) | can't test agents | Record a whole run once (LLM + tool calls), replay it forever — offline, deterministic. |
| [acttrace](acttrace.md) | no audit trail | Tamper-evident, offline-verifiable decision log + policy flags, with compliance evidence packs. |
| [core](core.md) | the shared glue | Types, token counting, offline-first prices, the `instrument()` seam, and the event bus every tool rides. |

```
contextkit  →  squeeze  →  tokenguard  →  cassette  →  acttrace
 assemble       compress      budget         test         audit
```

All six are **published on PyPI** and green in CI (offline tests · ruff · mypy).

## Install

```bash
pip install cendor            # the whole stack
pip install cendor-tokenguard # or any single tool (pulls cendor-core transitively)
```

Every package imports under the `cendor.*` namespace.

> **Prefer to read code?** The [Cookbook](/cookbook) has the full-stack support agent — one
> `instrument()` call, all six tools cooperating — as one copy-paste block.

## Where to go next

- **[Getting Started](getting-started.md)** — install, the one idea (`instrument` once), and a first budgeted, audited call.
- **[Architecture](architecture.md)** — the layers, the `instrument()` seam, the event bus, and the dependency graph.
- **[Providers & Integration](providers.md)** — OpenAI / Anthropic / Bedrock / Gemini / Ollama, plus managed runtimes via OpenTelemetry.
- **[Guides & Recipes](guides.md)** — copy-paste recipes, including the full-stack support agent.
- **[Benchmarks](benchmarks.md)** — reproducible, offline numbers for every package.
- **[FAQ](faq.md)** — common questions.
