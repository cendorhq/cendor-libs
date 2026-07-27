# Providers & Integration

`instrument()` identifies a client by its **shape**, not by model name — so new models from a
provider work the day they ship. It supports six providers directly — **OpenAI, Anthropic, Hugging
Face, Google Gemini, AWS Bedrock, and Ollama** — an OpenTelemetry ingestion path for managed
runtimes, and a callback handler for **LangChain / LangGraph** (see
[Frameworks](#frameworks-langchain--langgraph)).

## How detection works

```mermaid
%%{init: {"flowchart": {"htmlLabels": false}} }%%
graph TD
    A["instrument(client)"] --> B{"client has…"}
    B -->|"chat_completion (InferenceClient)"| HF["huggingface"]
    B -->|"chat.completions.create"| OAI["openai"]
    B -->|"responses.create"| OAI
    B -->|"messages.create"| ANT["anthropic"]
    B -->|"converse"| BR["bedrock"]
    B -->|"generate_content (GenerativeModel)"| GEM["google"]
    B -->|"models.generate_content (google-genai)"| GEM
    B -->|"chat callable"| OLL["ollama"]
    B -->|"none of the above"| NOOP["returned untouched"]

    classDef seam fill:#2563EB,color:#ffffff,stroke:#1E40AF;
    class A seam;
```

An OpenAI client exposes both `chat.completions.create` and `responses.create`; a `google-genai`
`Client` exposes both `models.generate_content` and `aio.models.generate_content`. `instrument()`
wraps **every** entrypoint it finds, so whichever API your code calls is captured.

## Per-provider setup

> **TypeScript.** `instrument()` detects all six providers in both languages — **OpenAI (Chat +
> Responses), Anthropic, Hugging Face, google-genai, Bedrock, and Ollama** — plus the OpenTelemetry
> ingestion path. The **LangChain / LangGraph** callback handler now ships in both languages too
> (`@cendor/core/langchain`). One thing stays Python-only: aws-sdk-v3 Bedrock (`instrument()` matches
> a boto-shaped `converse()`; the `send(ConverseCommand)` client rides the SDK provider). See the
> [parity matrix](languages.md).

### OpenAI (Chat Completions + Responses API + Embeddings)
`instrument()` wraps all three entrypoints; the Responses API reports usage differently, and it's
all normalized into the same `Usage`.

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from openai import OpenAI
from cendor.core import instrument
client = instrument(OpenAI())                       # env: OPENAI_API_KEY
client.chat.completions.create(model="gpt-4o", messages=[...])   # Chat Completions
client.responses.create(model="gpt-4o", input="…")               # Responses API (also captured)
client.embeddings.create(model="text-embedding-3-small", input="…")  # Embeddings (also captured)
```

<!-- tab: TypeScript -->

```ts
import OpenAI from 'openai';
import { instrument } from '@cendor/core';
const client = instrument(new OpenAI());            // env: OPENAI_API_KEY
await client.chat.completions.create({ model: 'gpt-4o', messages: [/* ... */] });  // Chat Completions
await client.responses.create({ model: 'gpt-4o', input: '…' });                    // Responses API (also captured)
await client.embeddings.create({ model: 'text-embedding-3-small', input: '…' });   // Embeddings (also captured)
```

<!-- /tabs -->

Embedding calls (since core 1.6.0 / 0.6.0) emit an `LLMCall` with `metadata["embedding"] = True`,
ride the same pre-flight interceptor pass (budgets can block, guards can redact-before-send), and
are priced from the snapshot's `text-embedding-*` rows. Azure OpenAI shares the client shape, so
its embeddings are captured the same way.
The Responses API (default for new OpenAI apps and the Agents SDK) reports `input_tokens`/
`output_tokens`, with cached tokens under `input_tokens_details.cached_tokens` and reasoning under
`output_tokens_details.reasoning_tokens` — all normalized.

### Anthropic

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from anthropic import Anthropic
client = instrument(Anthropic())                    # env: ANTHROPIC_API_KEY
client.messages.create(model="claude-sonnet-4-6", max_tokens=256, messages=[...])
```

<!-- tab: TypeScript -->

```ts
import Anthropic from '@anthropic-ai/sdk';
import { instrument } from '@cendor/core';
const client = instrument(new Anthropic());         // env: ANTHROPIC_API_KEY
await client.messages.create({ model: 'claude-sonnet-4-6', max_tokens: 256, messages: [/* ... */] });
```

<!-- /tabs -->

### Azure AI Foundry (models via the OpenAI SDK)
Detected as `openai` (same SDK shape). For the Foundry **Agent Service** (server-side loop), don't
`instrument()` — ingest its telemetry (see [Managed runtimes](#managed-runtimes-opentelemetry-ingestion)).

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from openai import AzureOpenAI
client = instrument(AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-10-21"))
client.chat.completions.create(model="<your-deployment-name>", messages=[...])  # detected as openai
```

<!-- tab: TypeScript -->

```ts
import { AzureOpenAI } from 'openai';
import { instrument } from '@cendor/core';
// AzureOpenAI has the same chat.completions.create shape, so it's detected as openai:
const client = instrument(new AzureOpenAI({
  endpoint: process.env.AZURE_OPENAI_ENDPOINT,
  apiKey: process.env.AZURE_OPENAI_API_KEY,
  apiVersion: '2024-10-21' }));
await client.chat.completions.create({ model: '<your-deployment-name>', messages: [/* ... */] });
```

<!-- /tabs -->

### Google Gemini
Both SDKs are detected — the current `google-genai` (model from the kwarg) and the legacy
`google-generativeai` (model read from the `GenerativeModel` object).

<!-- tabs: lang -->
<!-- tab: Python -->

```python
# Current SDK (google-genai) — the recommended shape:
from google import genai
client = instrument(genai.Client())                 # env: GOOGLE_API_KEY / GEMINI_API_KEY
client.models.generate_content(model="gemini-1.5-pro", contents="…")
await client.aio.models.generate_content(model="gemini-1.5-pro", contents="…")  # async also wrapped
```
```python
# Legacy SDK (google-generativeai) — still detected:
import google.generativeai as genai
genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
model = instrument(genai.GenerativeModel("gemini-1.5-pro"))
model.generate_content("…")     # model id read from the GenerativeModel, so the call is priced
```

<!-- tab: TypeScript -->

```ts
// Current SDK (@google/genai) — the model rides the `model` kwarg:
import { GoogleGenAI } from '@google/genai';
import { instrument } from '@cendor/core';
const client = instrument(new GoogleGenAI({ apiKey: process.env.GOOGLE_API_KEY }));
await client.models.generateContent({ model: 'gemini-1.5-pro', contents: '…' });  // detected as google
```

<!-- /tabs -->

### AWS Bedrock (Converse API)

<!-- tabs: lang -->
<!-- tab: Python -->

```python
import boto3
client = instrument(boto3.client("bedrock-runtime", region_name="us-east-1"))
client.converse(modelId="anthropic.claude-…",
                messages=[{"role": "user", "content": [{"text": "…"}]}])   # AWS credentials
```

<!-- tab: TypeScript -->

> **Bedrock in TypeScript.** `@cendor/core`'s `instrument()` **ships** Bedrock detection — it matches a
> **boto-shaped `converse()`** method, so a wrapper client that exposes `converse(...)` directly is
> captured. The official `@aws-sdk/client-bedrock-runtime` v3 has **no such method**: it issues calls
> generically as `client.send(new ConverseCommand(...))`, and `send` is shared by every AWS command, so it
> can't be duck-typed. aws-sdk-v3 Bedrock is therefore captured via the **SDK provider** (`@cendor/sdk`
> wraps the client directly), not `instrument()`. See the [parity matrix](languages.md).

<!-- /tabs -->

### Ollama (local, free)

<!-- tabs: lang -->
<!-- tab: Python -->

```python
import ollama
client = instrument(ollama.Client())
client.chat(model="llama3", messages=[...])   # no key
```

<!-- tab: TypeScript -->

```ts
import { Ollama } from 'ollama';
import { instrument } from '@cendor/core';
const client = instrument(new Ollama());
await client.chat({ model: 'llama3.2', messages: [{ role: 'user', content: '…' }] });   // no key
```

<!-- /tabs -->

### Hugging Face
`huggingface_hub`'s `InferenceClient` exposes `chat_completion(...)`, whose response is
OpenAI-shaped. `instrument()` binds to it **before** the client's OpenAI-compatible
`chat.completions.create`, so the call is attributed to `huggingface` rather than `openai`. The
model is a Hub id or an Inference Endpoint URL.

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from huggingface_hub import InferenceClient
client = instrument(InferenceClient())          # env: HF_TOKEN / HUGGINGFACEHUB_API_TOKEN
client.chat_completion(
    model="meta-llama/Llama-3.1-8B-Instruct",
    messages=[{"role": "user", "content": "…"}])   # OpenAI-shaped; attributed to huggingface
```

<!-- tab: TypeScript -->

<!-- ts-check: skip -->

```ts
import { InferenceClient } from '@huggingface/inference';
import { instrument } from '@cendor/core';
const client = instrument(new InferenceClient(process.env.HF_TOKEN));   // env: HF_TOKEN
await client.chatCompletion({
  model: 'meta-llama/Llama-3.1-8B-Instruct',
  messages: [{ role: 'user', content: '…' }] });   // OpenAI-shaped; attributed to huggingface
```

<!-- /tabs -->

## Managed runtimes (OpenTelemetry ingestion)

When a runtime owns the agent loop server-side and only emits `gen_ai.*` spans, feed the span
attributes to `core.otel.ingest(...)` so the call still lands on the bus — and `tokenguard` /
`acttrace` consume it as usual:

<!-- tabs: lang -->
<!-- tab: Python -->

```python
from cendor.core import otel
otel.ingest({
    "gen_ai.system": "azure_ai_foundry",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.usage.input_tokens": 1000,
    "gen_ai.usage.output_tokens": 500,
})   # -> emits a normalized LLMCall
```

<!-- tab: TypeScript -->

```ts
import { otel } from '@cendor/core';
otel.ingest({
  'gen_ai.system': 'azure_ai_foundry',
  'gen_ai.request.model': 'gpt-4o',
  'gen_ai.usage.input_tokens': 1000,
  'gen_ai.usage.output_tokens': 500,
});   // -> emits a normalized LLMCall
```

<!-- /tabs -->

`contextkit` / `squeeze` apply only when **you** assemble the prompt; if a managed runtime owns
context internally, those two have nothing to shape while the other three still work.

## Frameworks (LangChain / LangGraph)

For a framework, the SDK-aligned integration point is its **callback system**, not client wrapping —
and that recommendation is unchanged, though the reason has narrowed. `langchain_openai` reaches the
client through `with_raw_response`: a plain call
(`client.chat.completions.with_raw_response.create(...).parse()`) **is** captured and priced from
`cendor-core` 1.14.1, and its structured-output branch
(`chat.completions.with_raw_response.parse(...)`, taken whenever `response_format` is set) from
1.14.2. What inner-client wrapping still does **not** see is the **streaming** branch, which reads
the body through the envelope's own context manager. So use the callback handler: it is the only
integration that covers every branch, and it carries the framework's `run_id` for correlation.

<!-- tabs: lang -->
<!-- tab: Python -->

```python
pip install "cendor-core[langchain]"
```

```python
from cendor.core.langchain import CendorCallbackHandler
from langchain_openai import ChatOpenAI

handler = CendorCallbackHandler()
llm = ChatOpenAI(model="gpt-4o", callbacks=[handler])     # every call recorded onto the bus
llm.invoke("hi")

# LangGraph: attach once via config — it propagates to every node + tool, correlated by run:
agent.invoke({"messages": [...]}, config={"callbacks": [handler]})
```

<!-- tab: TypeScript -->

```bash
npm install @langchain/core
```

<!-- ts-check: skip -->

```ts
import { CendorCallbackHandler } from '@cendor/core/langchain';
import { ChatOpenAI } from '@langchain/openai';

const handler = new CendorCallbackHandler();
const llm = new ChatOpenAI({ model: 'gpt-4o', callbacks: [handler] });  // every call recorded onto the bus
await llm.invoke('hi');

// LangGraph: attach once via config — it propagates to every node + tool, correlated by run:
await agent.invoke({ messages: [...] }, { callbacks: [handler] });
```

> **Recording-only in TypeScript too**, exactly as in Python: it observes, it never enforces.

<!-- /tabs -->

The handler reads LangChain's own `usage_metadata` (which carries **reasoning** and **cached**
tokens), prices each call offline, emits normalized `LLMCall`/`ToolCall`, and stamps a
**root-run `trace_id`** so every model/tool call of one `agent.invoke` shares an id (separate
invocations get distinct ones). `tokenguard` and `acttrace` then consume these like any other bus
event — with no client touch.

**It is recording-only.** The callback path is post-call, so *enforcement* — `tokenguard`'s
`on_exceed="block"`, `acttrace`'s `guard()` redact-before-send — is a **no-op** here (those act on
the `instrument()` seam, which this path never touches). For enforcement, call the **provider SDK
directly** and `instrument()` it.

| Capability | Callback handler (LangChain/LangGraph) | Direct provider SDK + `instrument()` |
|---|---|---|
| Usage + cost | ✅ (from `usage_metadata`) | ✅ |
| Reasoning tokens | ✅ | ✅ |
| Tool calls (`ToolCall`) | ✅ | ✅ (`@instrument_tool`) |
| Multi-node / multi-agent `trace_id` | ✅ (root-run id, automatic) | ✅ via `core.trace("run-id")` |
| Pre-flight **enforcement** (block / downgrade / clamp / redact-before-send) | ❌ recording-only | ✅ |
| Record/replay (`cassette`) | ❌ | ✅ |

## Live pricing

Cost is computed from a price table. Which providers actually let you refresh it live varies. The bundled snapshot works offline; `prices.refresh(source=…)`
pulls live rates. But the **direct model labs publish no pricing API** — their model-list endpoints
return ids only — so "ask the provider for today's price" only works for gateways, cloud catalogs,
and aggregators.

| Source | Live pricing API? | Auth | Built-in adapter |
|---|---|---|---|
| OpenAI / Anthropic (direct) | ❌ — `/v1/models` lists ids, no rates | — | use LiteLLM instead |
| **LiteLLM** `model_prices_and_context_window.json` | ✅ static JSON, ~daily, all providers | none | `refresh(source="litellm")` |
| **OpenRouter** `/api/v1/models` | ✅ per-token JSON | none | `refresh(source="openrouter")` |
| **Azure Retail Prices** | ✅ `retailPrice`/`unitOfMeasure` | none | `refresh(source="azure")` |
| AWS Bedrock / GCP Vertex | ✅ Price List / Billing Catalog | creds/SDK | bring your own `mapper=` |

The three built-in adapters are all **unauthenticated HTTPS GETs** — no credentials, no SDKs, no new
dependencies. AWS/GCP need credentials and SKU/region mapping, so they're intentionally out of core.
All refreshes are offline-safe and fall back to the last-good table silently. See
[core → Prices](core.md#prices).

A gateway that returns the **actual billed cost** on the response (e.g. OpenRouter's `usage.cost`) is
better than any table: `instrument()` uses that figure directly and labels the call `cost_reported`
(vs `cost_estimated` for a table estimate).

## Streaming

Streaming is supported for every provider: pass `stream=True` and the chunk iterator flows through
your code unchanged while usage is accumulated, so the call is still priced and recorded once the
stream completes. How real (vs estimated) the streamed usage is depends on the entrypoint:

- **OpenAI Chat Completions** — `instrument()` auto-requests a final usage chunk
  (`stream_options={"include_usage": True}`, unless you set `stream_options` yourself), so streamed
  usage is the provider's **real billed count**.
- **OpenAI Responses API** — usage rides the `response.completed` event, so nothing is injected.
- **Hugging Face** — `instrument()` injects `stream_options={"include_usage": True}` **only when the
  installed `huggingface_hub`'s `chat_completion` signature explicitly accepts it** (Python; older
  hubs / TS are left untouched — pass it yourself where the router supports it). Since core 1.10.
- **Bedrock `converse_stream`** — captured in **Python** since core 1.10 (a Bedrock client exposes both
  `converse` and `converse_stream`; the latter has no `stream=` kwarg and returns the event iterable as
  the `"stream"` member of a dict response, which `instrument()` wraps and hands back unchanged). TS
  aws-sdk-v3 streaming still rides the SDK provider (the `send(ConverseCommand)` shape can't be
  duck-typed).
- **Other providers** — usage is read from the provider's own stream reporting where present, else an
  offline estimate flagged `usage_estimated`. The offline estimate now also counts **visible** thinking
  (Anthropic `thinking_delta`, Ollama `message.thinking`, OpenAI-compat `reasoning_content`, Bedrock
  `reasoningContent`); **hidden** reasoning (OpenAI-native, Gemini) never reaches the wire and stays
  invisible.

**Mid-stream budget cut:** `tokenguard`'s `budget(on_exceed="break")` rides core's per-chunk
stream-observer seam (`add_stream_observer`, core 1.10 / 0.11) to cut a runaway *stream* the instant its
running estimate crosses the cap — see [tokenguard streaming runaways](tokenguard.md#streaming-runaways-on_exceedbreak).

## Notes

- Pricing for a model is looked up in the bundled snapshot; an unpriced model yields `cost = None`
  (the call still works). Add rates with `prices.refresh()`.
- New model ids need no library release — capture is by client shape, and pricing is a data table.
