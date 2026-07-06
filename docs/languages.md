# Languages & parity

Cendor ships in two languages: **Python** (`cendor.*`, the reference implementation, on PyPI) and
**TypeScript/JavaScript** (`@cendor/*`, on npm — ESM-only, Node LTS first, edge runtimes
supported). Both are implementations of the same versioned [format specs](https://github.com/cendorhq/cendor-libs/tree/main/docs/specs),
so the artifacts that matter — cassettes, audit chains, price tables, bus events — are
**byte-for-byte interoperable** and enforced by conformance vectors committed to both CIs.

<!-- tabs: lang -->
<!-- tab: Python -->

```bash
pip install cendor-libs        # the whole stack; `cendor` is an alias
```

```python
from cendor.core import instrument
client = instrument(OpenAI())
```

<!-- tab: TypeScript -->

```bash
npm i @cendor/libs             # the whole stack (umbrella)
```

```ts
import { instrument } from '@cendor/core';
const client = instrument(new OpenAI());
```

<!-- /tabs -->

## One API, two spellings

The TypeScript API is *derived from* the Python public API, mechanically:

| Rule | Python | TypeScript |
|---|---|---|
| Names | `snake_case` (`max_turns`, `on_exceed`) | `camelCase` (`maxTurns`, `onExceed`) |
| Scopes | context managers (`with budget(...):`) | async callbacks (`withBudget(cfg, fn)`) / decorators (`budget(cfg)(fn)`) |
| Money | `Decimal` — never a float | `decimal.js` — never a float; value-equal across languages |
| Errors | `BudgetExceeded`, `PolicyViolation`, … | identical names |
| Defaults | the same | the same |
| Tool schemas (SDK) | derived from type hints + docstring | declared with [zod](https://zod.dev) |

## Cross-language guarantees

These are tested by committed conformance vectors, not promised:

- A **cassette** recorded in Python **replays in TypeScript** (and vice-versa) — same request
  hashes, same wire format.
- An **audit chain** written in TypeScript **`verify()`s in Python** — identical canonical bytes
  and HMAC inputs.
- **Prices and token counts match exactly** — same bundled snapshot, `tiktoken` ↔ `js-tiktoken`
  parity, decimal-exact math.
- **Bus events** (`LLMCall` / `ToolCall` / `Usage` / `Money`) share one schema across languages.

## Parity matrix — libraries

Legend: ✅ ported · 🚧 partial/scoped · **Py-only** deliberately not ported.

| Capability | Python | TypeScript | Notes |
|---|---|---|---|
| `Money` (decimal, never float) | ✅ | ✅ | `Decimal` ↔ `decimal.js`; value-equal across langs |
| `Usage` / `LLMCall` / `ToolCall` | ✅ | ✅ | snake_case ↔ camelCase fields; type names identical |
| Event bus | ✅ | ✅ | subscribe/emit/unsubscribe; error isolation |
| Price table + `estimate()` | ✅ | ✅ | same bundled snapshot; `refresh()` async in TS |
| Token counting | ✅ | ✅ | `tiktoken` ↔ `js-tiktoken` — exact counts match |
| `instrument()` providers | ✅ 6 providers | 🚧 OpenAI (Chat + Responses) + Anthropic | same seam; more land by demand |
| `instrument()` streaming / interceptors | ✅ | ✅ | |
| `trace()` correlation | ✅ contextvars | ✅ AsyncLocalStorage | |
| **tokenguard** budgets / track / report / sinks | ✅ | ✅ | SQLite / Queue / OTel sinks in both |
| **contextkit** assemble / evict / order | ✅ | ✅ | TS collapses sync+async into one `async assemble()` |
| **squeeze** compress / decompress | ✅ | ✅ | deterministic; handle ids match |
| **cassette** record / replay | ✅ | ✅ | cross-language replay, vector-verified |
| cassette storage | fs | fs + memory (+ IndexedDB-shaped) | pluggable adapters |
| **acttrace** chain / verify / sign | ✅ | ✅ | cross-language verify (HMAC + `_meta`) |
| acttrace detectors | ✅ regex **+ Presidio NER** | ✅ regex/pattern (20 detectors) | **NER is Py-only** — `ner_available()` → `false` in TS |

## Parity matrix — SDK

| Capability | Python | TypeScript |
|---|---|---|
| `Agent` / `tool` / `run` / `Result` | ✅ | ✅ (zod tool schemas) |
| Providers | ✅ ten paths | 🚧 OpenAI (Chat + Responses) + Anthropic first |
| Sessions & memory | ✅ (+ SQLite store) | ✅ (better-sqlite3 + memory adapters) |
| Handoff / supervisor / pipelines | ✅ | ✅ |
| Structured output / streaming | ✅ | ✅ |
| Governance re-exports | ✅ | ✅ (the real `@cendor/*` objects) |
| Live progress / prompt caching / live OTel spans | ✅ | ✅ |

## Runtime targets (TypeScript)

| Package | Node | Edge (Workers) | Browser |
|---|---|---|---|
| `@cendor/core` | ✅ | ✅ | 🚧 types/bus/prices/tokens are pure; `instrument` wraps fetch SDKs |
| `@cendor/contextkit`, `@cendor/squeeze` | ✅ | ✅ | ✅ pure compute |
| `@cendor/tokenguard` | ✅ | ✅ | ⚠️ advisory only — enforcement is server-side |
| `@cendor/cassette` | ✅ (fs) | ✅ (adapter) | ⚠️ memory/IndexedDB adapter |
| `@cendor/acttrace` | ✅ | ✅ | ❌ never — signing keys can't live in a client |
| `@cendor/sdk` | ✅ | ✅ (HTTP/SSE transports; MCP stdio is Node-only) | ❌ keys-in-browser anti-pattern |

> **Governance is only real where the user can't tamper with it.** Budgets-as-enforcement,
> audit-as-evidence, and redaction-as-guarantee are server-side by definition — in every
> language. Browser builds of the pure-compute libraries are UX aids (live token/cost preview,
> context assembly in the chat UI), and that's all they claim to be.

## Honest limits

- **Versions are independent across languages.** Python and TypeScript release on their own
  cadence; this page — not matching version numbers — is the parity contract.
- **The TS provider surface is narrower today** (OpenAI + Anthropic). The seam is identical, so
  breadth is a porting exercise, not a design one.
- **No Presidio NER in TypeScript** — regex/pattern detectors only, and
  `ner_available()` says so at runtime.
- **Docs code samples default to Python** where a tab pair isn't shown; the mapping rules above
  translate mechanically.
