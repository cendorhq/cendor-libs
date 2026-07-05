# API parity rules

**Spec version:** `parity/1` · **Status:** stable · **Applies to:** every Cendor package and `cendor-sdk`,
across languages.

This spec defines how the Cendor public API maps **mechanically** from the reference Python
implementation to any other language (initially TypeScript, `@cendor/*`). The goal is that a developer
who knows one language's API can predict the other's without a lookup, and that documentation can carry
both in synchronized code tabs. If a port invents its own shapes, the single-source docs model
collapses — so these rules are binding, not advisory.

The **reference surface** is the Python public API. For the SDK specifically, the canonical symbol list
is the public API table in the `cendor-sdk` repo's `docs/index.md`; the TypeScript API is *derived from*
it by applying the rules below. New public symbols are added to Python first (or to both at once), never
to a port alone.

## 1. Package & import mapping

One rule across ecosystems, single-level everywhere:

```
distribution  cendor-<x>   ↔   npm  @cendor/<x>   ↔   import  cendor.<x>  /  @cendor/<x>
```

- `cendor-core` ↔ `@cendor/core` ↔ `cendor.core` / `import … from '@cendor/core'`.
- `cendor-sdk` ↔ `@cendor/sdk` ↔ `cendor.sdk`.
- Umbrella `cendor-libs` ↔ `@cendor/libs`; brand alias `cendor` (PyPI) ↔ unscoped `cendor` (npm, published as a real pointer, never an empty placeholder).
- **Never nest** (`cendor.libs.core` is forbidden) — npm scopes are single-level, so only the flat rule mirrors 1:1.

## 2. Identifier casing

| Kind | Python | TypeScript | Rule |
|---|---|---|---|
| Function / method | `snake_case` | `camelCase` | mechanical: `run`, `require_approval` → `run`, `requireApproval` |
| Keyword argument | `snake_case` kwarg | field of an options object, `camelCase` | `max_turns=` → `{ maxTurns }`; `output_type=` → `{ outputType }`; `on_exceed=` → `{ onExceed }`; `group_by=` → `{ groupBy }` |
| Class / type | `PascalCase` | `PascalCase` | **identical**: `Agent`, `Session`, `RetryPolicy`, `AuditLog`, `Policy`, `VectorIndex` |
| Error / exception | `PascalCase` | `PascalCase` | **identical name**: `BudgetExceeded` is `BudgetExceeded` in both |
| Constant / enum-like string | value preserved | value preserved | `on_exceed="block"` ↔ `onExceed: "block"` — the *string values* never change |

Only argument/method **spelling** changes (snake↔camel). Type names, error names, and string literal
values are byte-identical so error handling and config port without translation.

## 3. Argument passing

- Python **keyword arguments** map to a single trailing **options object** in TypeScript. Required
  positional args stay positional: `run(agent, "hello", max_turns=3)` ↔ `run(agent, "hello", { maxTurns: 3 })`.
- **Defaults are identical.** If Python's `run` defaults `max_turns=8`, TypeScript defaults `maxTurns` to
  `8`. A port may not silently pick a different default; a default change is a spec change in both.

## 4. Language-idiom equivalences

Some Python constructs have no literal TS twin; each has one **designated** equivalent so the mapping
stays predictable:

| Python | TypeScript | Notes |
|---|---|---|
| Context manager (`with budget(...): …`, `with guard(...)`, `with track(...)`, `with trace(id)`) | async callback scope (`await withBudget({...}, async () => { … })`) or a `Disposable`/`using` where a block scope is natural | The **effect** (ambient budget/attribution/audit/correlation for the enclosed work) is identical; only the syntax differs. |
| Decorator (`@tool`) | factory call (`tool({ description, parameters, execute })`) | TS has no runtime type hints, so tool parameter schemas are declared with **zod** (`parameters: z.object({…})`) — the idiomatic equivalent of Python deriving JSON Schema from type hints. |
| Sync + async pair (`run(...)` and `run.aio(...)`, `assemble` / `aassemble`) | single async function | JS is async-first; the port exposes only the async form. This asymmetry is expected and documented, not a parity violation. |
| `dataclass` / Pydantic model as `output_type` | zod schema or TS type + zod | Structured-output target; same three acceptance modes (schema object, JSON-schema dict, native type) where the language allows. |
| Keyword-only booleans (`stream=True`) | option field (`{ stream: true }`) | — |

## 5. Wire & value contracts (defer to the other specs)

Parity of *shapes* is not enough; the *bytes* must match too. Where an API surfaces a cross-language
artifact, the relevant spec governs:

- Money / cost values → [Bus event shapes](bus-events.md) and never IEEE float in any language.
- Recorded runs → [Cassette file format](cassette-format.md).
- Audit chains → [acttrace chain](acttrace-chain.md).
- Event vocabulary (`LLMCall`/`ToolCall`/`Usage`/`Money`) → [Bus event shapes](bus-events.md).

## 6. Enforcement

- **Parity matrix** (a docs page, maintained per feature): feature × language, ✅ / 🚧 / —. Updating it
  is part of a feature PR's definition-of-done. An unported feature is marked, not hidden.
- Docs use synchronized language tabs; a tab that isn't at least typechecked against the published
  package is assumed to rot, so ported snippets are CI-checked.
- A symbol that exists in a port but not in the Python reference is a bug in the port, not a new feature.
