# Changelog — cendor-core

All notable changes to this package are documented here. Format: [Keep a Changelog](https://keepachangelog.com); this project follows [Semantic Versioning](https://semver.org) — minor releases are additive and backward-compatible, and breaking changes land only in a new major.

## [1.20.0] — 2026-08-02

### Changed — an absent price rate is **unknown**, never zero

1.19.2 closed the *data* half of this: the feed can no longer publish a row without an output rate.
This closes the two halves a data fix cannot reach — the **spec** and the **library** — so a table
that did not come from us can no longer make the same mistake.

`prices/1` used to read an absent `output` as `0`. Right for an embedding, which genuinely bills no
output tokens; wrong for a chat model whose rate merely failed to parse — and downstream the two are
indistinguishable, so `estimate()` reported a fabricated `$0.00` as a *fact* and a USD `budget(...)`
cap under-counted by the entire output side.

Measured on 1.19.2 itself, through a documented API:

```
refresh(source="litellm")   ->  10 rows with no output rate
estimate("gpt-image-1", 1_000_000, 1_000_000)   ->  $5.00
OpenAI's own rates ($5/1M text in, $40/1M image out)  ->  $45.00
```

`refresh(source="azure")` supplied one more (`fw-deepseek-v4-pro-ch`).

- **New `prices.MissingRateError`**, a **subclass of `UnknownModelError`** (and so of `KeyError`), so
  every existing handler is unaffected — `instrument()`, `otel`, the LangChain handler and
  `tokenguard` all already catch it and fall back to an honest `None` / warn-once. Catch the new type
  only to tell *"no such model"* from *"known model, unusable rate"*. Its message names the fix, in
  your own code, on both call shapes.
- **`estimate()` refuses an unpriceable rate object** whenever it prices the model — *not* only when
  the call carries output tokens. A table that cannot price a model cannot price it, and learning
  that on the first output-bearing call is a late, partial signal. Three shapes are refused: no
  `input`, a **table-stated** zero `input` (previously a silent `$0.00`; a missing `input` was a bare
  `KeyError`), and no `output`.
- **An explicit `"output": 0` is honoured forever** — 18 rows in the bundled snapshot are real
  embeddings and depend on it.
- **A rate *you* registered is never second-guessed.** `prices.register("llama3", {"input": 0,
  "output": 0})` still prices a local model at zero, exactly as 1.19.0 documented: the spec already
  says a user registration outranks any table, and a zero a person wrote is a statement, while a zero
  that arrived inside a fetched table is a parser having lost one.
- **`register_deployment(like=)` fails at registration** rather than on the first call when the base
  it is copying cannot price one.
- **A mapped `refresh(source=…)` drops rows it cannot price** — the library mirror of the feed's own
  `dropZeroInput` + `dropMissingOutput`. Such a model is then honestly *absent* (the plain
  `UnknownModelError` a caller already handles) instead of surviving half-priced. A pass-through
  `refresh(url=…)` is a **table**, not a mapper: every row a user's own table states is kept, and
  `estimate()` refuses the unpriceable ones by name.
- **There is deliberately no switch back to the old behaviour.** The escape is to *state* the rate —
  `prices.register_model_price(model, input=…, output=…, per="1M")`, or an explicit `0`.

Why a **minor** and not a major: the shape of `prices/1` is unchanged, the function's contract
("the cost of this call") is unchanged, the error is one the API already defines and documents for
this condition, and **zero** rows in the bundled snapshot, the feed or `refresh(source="modelsdev")`
are affected. It is the same call 1.19.0 made when it removed `llama3` at `0.0/0.0` from the snapshot
and turned `estimate("llama3", …)` from `$0.00` into an `UnknownModelError` — one field over.

Spec: `docs/specs/price-dataset.md` § *Changed 2026-08-02*. Parity: `@cendor/core` 3.7.0.

## [1.19.2] — 2026-08-02

### Fixed
- **A missing output rate no longer prices a chat model as free.** The bundled snapshot is
  regenerated from a feed that can no longer publish a row without one. `prices/1` reads an absent
  `output` as **zero** — correct for an embedding, which bills no output tokens, and wrong for a
  chat model whose rate simply never parsed, because `estimate()` then reports the output side as a
  *fact* of `$0.00` and a USD `budget(...)` cap under-counts by the entire output cost.

  14 rows in the 1.19.1 snapshot were affected. Three now carry a real rate:

  | model | output was | output now |
  |---|---|---|
  | `claude-3-haiku` | `$0.00` | `$1.25` / 1M |
  | `claude-3-sonnet` | `$0.00` | `$15.00` / 1M |
  | `gpt-image-2` | `$0.00` | `$30.00` / 1M |

  `estimate("claude-3-haiku", 1_000_000, 1_000_000)` returned `0.25` and now returns `1.50`. **If you
  budget or report on any of those three, your figures were low by the output side** — the input
  side, and every other model, was always correct.

  Twelve rows no source prices an output rate for are now **absent** rather than free, which the
  libraries render as an honest `None` plus a warn-once: `claude-2-0`, `claude-2-1`, `claude-instant`,
  `az-gpt4-turbo-128k`, `gpt-image-1-mini`, `chatgpt-image-latest`, `gpt-4o-transcribe-diarize`,
  `mai-image-2.5`, `mai-image-2.5-flash`, `mai-image-2e`, `codestral-embed`, `codestral-embed-2505`.
  An output rate a source explicitly *states* as `0` is untouched — real embeddings keep theirs.

- **The snapshot's `_feed` field named a URL that 404s.** It still pointed at
  `raw.githubusercontent.com/cendorhq/cendor-prices`, which requires auth now the repo is private.
  It names the Pages feed, as `SNAPSHOT_URL` already did in 1.19.1.

Snapshot: 861 → 849 rows, `_updated` 2026-08-02.

## [1.19.1] — 2026-08-02

### Fixed
- **`SNAPSHOT_URL` moves to the feed's GitHub Pages URL**
  (`https://cendorhq.github.io/cendor-prices/prices.json`). The `cendorhq/cendor-prices` repo is
  private — the builder, the curation policy and the run history are internal — so the
  `raw.githubusercontent` URL 1.19.0 shipped requires auth and would 404. A data-only `gh-pages`
  branch publishes the file itself, keyless and CDN-served, and Pages returns it as
  `application/json` rather than raw's `text/plain`. **Anyone on 1.19.0 should upgrade**: there, a
  bare `prices.refresh()` fails and — because `refresh()` is contractually never-raise — returns a
  silent `False`, leaving the bundled snapshot active. Nothing is wrong with the rates in 1.19.0;
  only the default refresh target is unreachable.

## [1.19.0] — 2026-08-02

Live pricing: three new first-party/aggregator sources, a rewritten Azure source, and the
visibility layer that makes any rate explain itself. Additive and backward-compatible.

### Added
- **`prices.refresh(source="aws")`** — the AWS Bedrock **public price files**, Amazon's own billing
  catalog. Keyless, dated from `publicationDate`, one region (`region=`, default `us-east-1`).
  ⚠️ It unions **both** offer codes, and that is not defensive coding: measured 2026-08-01,
  `AmazonBedrock` alone carries only Claude 2.0/2.1/3-Haiku/3-Sonnet/Instant — `Claude Sonnet 4` and
  `4.5` exist **only** in `AmazonBedrockService`, so a single-offer client silently misses every
  current Claude rate. Rate keys come from `usagetype`, not `inferenceType`, because Sonnet 4 carries
  `"Input tokens"` on both the standard meter ($3/MTok) and the half-price batch one.
- **`prices.refresh(source="modelsdev")`** — models.dev (MIT), the widest keyless catalog found
  (177 providers / 5,935 models). Per-1M rates converted exactly; per-row `last_updated` carried
  through as real provenance. ⚠️ Restricted to a first-party provider allowlist: the same model id
  appears under many providers at different prices (`gpt-5.1` under 11, from $1.07 to $1.25/MTok)
  and the providers with the most rows are all resellers.
- **`prices.refresh(source="vercel")`** — Vercel AI Gateway. Gateway **resale** prices, like
  OpenRouter's; base rates only (its tiered pricing is out of scope); undatable.
- **`prices.explain(model)` → `PriceExplanation`** — the resolved id, *how* it resolved
  (exact / normalized / registered / unpriced), the rates, the table's and the **row's** provenance,
  the age, and honest notes (a registration in effect, a resale source, an undatable table, an
  unpriced model). `.summary()` is one line for a log. Never raises.
- **`prices.save(path)` / `prices.load(path)`** — explicit, opt-in persistence of the active table
  across processes, carrying provenance and `_updated` through so `explain()` and `age_days()` stay
  honest after a load. `source()` then reports `"loaded"`. There is deliberately **no implicit
  cache**: a hidden one is how prices go invisibly stale.
- **`refresh(..., required=True)`** — raises the new `PriceRefreshError` instead of returning
  `False`. Never the default; `refresh()` stays contractually never-raise.
- **`prices.azure_url(region)`** and `region=` on `refresh()` for the `azure` / `aws` sources.

### Changed
- **`prices.refresh(source="azure")` is rewritten.** The filter is now
  `serviceName eq 'Foundry Models'` with a **mandatory region** and pagination. The pre-rename
  `productName eq 'Azure OpenAI'` still returned rows — which is exactly why the coverage loss was
  invisible — but saw 462 of eastus2's 1,526 meters and **no GPT-5, DeepSeek, Grok, Mistral, Llama,
  Phi, Kimi, Qwen or Cohere meter at all**. Measured end to end: **104 mapped models where the old
  filter mapped 23**. Two further fixes inside it: `opt` is read as **output** (141 rows spell it
  that way, so every GPT-5.x family previously had an input rate and no output rate), and batch /
  fine-tune / provisioned / long-context / media meters are excluded rather than winning a
  cheapest-rate comparison. The region term is not an optimisation — unregioned, the same query is
  more than 25,000 rows and still paging after ~28 s.
- **`SNAPSHOT_URL` points at the cendor-prices feed.** A bare `refresh()` now fetches a dated,
  per-row-provenanced table reconciled daily behind validation gates
  (`github.com/cendorhq/cendor-prices`), not this repo's own snapshot. Same schema; a freshness win.
- **The bundled `prices.json` is GENERATED, not hand-typed** (`scripts/sync_prices.py`), from that
  feed plus its reviewed curation policy. **44 hand-fed rows → 861 rows**, each carrying
  `_provenance`. The hand-feeding drift goes with it: `gpt-5.6-luna` was **5× off** every other
  source and `gpt-5.6-terra` 1.25×. A release gate now refuses a snapshot older than 30 days.
- **A zero input rate is never published.** `llama3` (0/0, inherited from litellm) leaves the
  snapshot: it made exactly one local model report a fabricated `$0.00` while every other reported
  `None`, and `estimate()` returning `$0.00` as a *fact* means a USD cap silently never binds. To
  price a local model at zero, say so — `prices.register("llama3", {"input": 0, "output": 0})`.
- Rates are coerced to `Decimal` once at the table swap, so `explain()` hands callers real
  `Decimal`s even for a pass-through `refresh(url=…)` against a table that quotes its rates.
- A litellm key namespaced to a **host** no longer overwrites the bare id. Measured:
  `vertex_ai/claude-3-5-haiku` is Vertex's $1/$5, not Anthropic's $0.80/$4.

### Docs
- `docs/core.md` §Prices rewritten: the precedence contract, the source table, `explain`,
  `save`/`load`, the staleness signal, and the honest "dated list prices" line.
- `docs/specs/price-dataset.md`: optional `_provenance` (additive, `prices/1`-compatible), the
  refresh-source table, and the zero-input-rate rule.

## [1.18.0] — 2026-08-01

One additive bus accessor. Backward-compatible.

### Added
- **`bus.has_subscribers()`** — `True` when at least one subscriber is registered. It exists so an
  emitter can skip *building* an expensive event nobody would receive: `cendor-squeeze` ≥ 1.1.2
  gates the two `tokens.count()` passes that fill its `CompressionEvent` on it (measured at ~93% of
  a large `compress()` with nothing listening). Advisory by design — a subscriber registered on
  another thread between the check and the `emit` misses that one event, which is benign (the event
  predates its subscription) — and it answers "is anyone on the bus", not "is anyone listening for
  this event type". The private `_subscriber_count()` test helper is unchanged.

## [1.17.0] — 2026-07-31

Two silent Anthropic bypasses closed, and the interceptor chain's ordering contract corrected.

### Added
- **`client.messages.stream(...)` is captured.** It was a documented silent bypass and the gap was
  total: measured on `anthropic` 0.120.2, it emitted **zero** `LLMCall`s through every one of its
  three consumption paths — iteration, `.text_stream`, and `.get_final_message()` — while the HTTP
  POST plainly happened. Reachability was never theoretical: Semantic Kernel's Anthropic connector
  streams through this helper. The cause is that `Messages.stream` does not delegate to
  `messages.create`; it builds its own `partial(self._post, "/v1/messages", …, stream=True)` and
  hands it to a `MessageStreamManager`, so the wrapped `create` had nothing to observe. It is now its
  own instrument target, and a *stream-manager* one: the request is issued by the manager's
  `__enter__`, so that is where the counting attaches. A `messages.stream()` call now runs pre-flight
  interceptors before the request (a budget block issues **zero** HTTP requests; a `guard()` sends the
  provider redacted messages), emits exactly one `LLMCall` on drain with the provider's own usage and
  a TTFT, and joins `trace()` / cassette like the other always-stream targets. You still get the
  SDK's genuine `MessageStream` back — core substitutes the one raw stream underneath it that every
  consumption path funnels through, rather than re-implementing the helper's surface.
- **`client.messages.parse(...)` is captured**, for the same reason: it POSTs its own request too, so
  Anthropic structured output emitted nothing at all before. This is the Anthropic twin of the openai
  `responses.parse` / `chat.completions.parse` gaps closed in 1.14.1 / 1.14.2.
- **`on_missing_compressor`** on `contextkit.Context` — see that package's changelog.

### Changed
- **A `Reroute` no longer ends the interceptor chain; only a returned *response* does.** The two
  return values mean different things: a recorded response (cassette's replay) means the provider is
  never called, so nothing is left to rewrite and stopping is correct — while a `Reroute` still goes
  to the provider, so every remaining interceptor must still be consulted, and against the rerouted
  call. Before this, `_intercept` returned the first non-MISS result and a `Reroute` is one, so **the
  first interceptor that rewrote a request silently skipped every one after it.** What that cost was
  silent and in the dangerous direction, measured: with a `tokenguard` clamp registered before an
  `acttrace.guard()`, the clamp fired and the PII went to the provider **unredacted**; registered the
  other way round, the guard fired and the token cap **silently never bound**. Which one you lost
  depended on registration order — something a user has no way to observe. Reroutes now compose in
  registration order (later wins on the same field), each interceptor sees the request as it will
  actually be sent, and a raise still stops everything. If you had code relying on one library
  shadowing another, stop registering the one you did not want.

### Fixed
- **`Reroute(model=…)` now lands on the provider's own model kwarg — `modelId` on Bedrock's Converse
  API.** It was assigned generically, so on Bedrock the rewrite went to a `model` member Converse
  does not have: measured, a lenient client sent the **original, expensive** model while the
  `LLMCall`, the budget ledger and the audit chain all recorded the cheap one, and real boto3 raised
  `Unknown parameter in input: {'model'}` and never made the call. Either way `on_exceed="downgrade"`
  did not downgrade on Bedrock. Found while analysing the ripple of the aws-sdk-v3 work, not from a
  report.

### Honest limits
- The `o200k` proxy's Claude undercount is now **measured** rather than quoted, and it is worse than
  Anthropic's own "~30%" wording suggests: against `messages.count_tokens` over 27 samples
  (prose/code/JSON × 3 sizes, message-level on both sides), Opus 4.7 / Sonnet 5 / Fable 5 come out at
  **1.49×** the proxy (range 1.32–1.66) and older ids such as Sonnet 4.5 / Haiku 4.5 at **1.14×**
  (1.03–1.22) — so the older ids are not exempt either. **No scaling factor is applied, deliberately:**
  the ratio tracks the content (JSON ~1.33, prose ~1.66), so a single number would over-count JSON by
  ~12% while under-counting prose by ~11%, and a confidently wrong count is worse than a documented
  estimate. `tokens.method()` keeps reporting `bpe-estimate`. `docs/core.md` carries the table and a
  verified `tokens.register("anthropic", …)` recipe for exact counts.
- `tool_runner` is no longer listed as a bypass because it no longer exists: `anthropic` 0.120.2
  exposes no such method on `client.messages`. The Batch APIs remain post-hoc only, by design.

## [1.16.0] — 2026-07-31

### Added
- **`prices.register_deployment(deployment, like="gpt-4o")`** — price an Azure / Azure AI Foundry
  **deployment name** by copying the rates of the base model it serves. On Azure the id a call reports
  is the deployment name *you* chose, not a model id, so it is in no price table: `cost` is `None`,
  `tokenguard` records `$0`, and a USD `budget(...)` silently never binds. You already know which model
  the deployment serves; this says so once, instead of making you find and re-type a rate card.

  Deliberately **explicit**: this is not the `-preview` / `-latest` alias *guessing* that was
  considered and rejected (a confidently wrong price is worse than an honest `None`) — nothing is
  inferred from the deployment's name. **Copy-at-registration, not a live alias:** `like`'s rates are
  read now and stored as the deployment's own registration, so a later `refresh()` that reprices the
  base does *not* reprice the deployment (call it again to pick that up), and — like every
  registration — it survives `refresh()` and overrides a snapshot row with the same id. `like` goes
  through the same lookup reduction a real call does, so a dated or Bedrock-decorated base id works.
  An unknown `like` (or one whose entry carries no `input` rate) **raises `UnknownModelError`** rather
  than leaving the deployment quietly unpriced — which would reproduce the exact silence the function
  exists to remove. Every rate key is copied, not an enumerated few, so a future rate category cannot
  be silently dropped. Re-exported as `cendor.sdk.register_deployment`; TypeScript parity is
  `prices.registerDeployment(deployment, { like })` in `@cendor/core` 3.2.0.
- **`otel.span(model, …, tracer=…)`** — emit the `gen_ai` span on a `Tracer` you own instead of the
  global provider. Omit it and nothing changes (still `trace.get_tracer("cendor.core")`); pass one for
  the three cases the global provider is wrong for: a **test** asserting spans without installing a
  process-global provider, a **multi-tenant host** with a provider per tenant, and a **second
  pipeline** beside the app's own. Span name, attributes, and the without-OpenTelemetry no-op are
  identical either way. Filed as a product improvement by the external black-box suite, whose keyless
  tree had to install a global provider purely to observe these spans.

## [1.15.0] — 2026-07-31

### Added
- **`prices.register(model, rates)` and `prices.register_model_price(model, input=…, output=…, per="1M")` are public.**
  Python core deliberately had *no* public price-registration API: `prices.register` raised a PEP 562
  pointer at `cendor.sdk.register_model_price`, so a **libraries-door** user had to install the SDK
  distribution to price one Azure deployment / fine-tune / Bedrock marketplace id. That asymmetry is
  gone — `@cendor/core` has had `prices.register` all along, and now so does this. Both forms write
  through the same seam, override a snapshot row with the same id, and **survive `refresh()`**.
  `cendor.sdk.register_model_price` becomes a thin re-export (identical signature, nothing to change
  in existing code), and the old private `prices._register` hook stays as a deprecated alias so an
  older pinned SDK keeps working. The PEP 562 hook now teaches near-misses (`set_price`,
  `registerModelPrice`, …) instead of denying the real name.
- **Gemini streaming is captured** — `client.models.generate_content_stream` and
  `client.aio.models.generate_content_stream` (google-genai). The SDK streams through a **separate
  method**, not a `stream=True` kwarg, so it needed its own always-stream target (the machinery
  Bedrock's `converse_stream` already uses); until now a streamed Gemini call emitted **nothing at
  all** — measured live 2026-07-31, sync and async, zero `LLMCall`s. One `LLMCall` on completion with
  `metadata["streamed"]`, real usage taken from the **last** chunk's `usage_metadata` (Gemini reports
  *running totals* on every chunk, so first-chunk-wins would under-count), `thoughts_token_count`
  folded into output and surfaced as `reasoning_tokens`, a flagged offline estimate when a stream
  reports no usage, chunks passed through unchanged, and the stream-observer seam firing per chunk —
  so `cendor-tokenguard`'s `budget(..., on_exceed="break")` cuts a runaway Gemini stream and closes it.

### Fixed
- **`prices.refresh(source="azure")` had never worked.** `AZURE_URL` carried raw spaces in its
  `$filter` query; `urllib.request.urlopen` rejects that outright (`InvalidURL: URL can't contain
  control characters`), and because `refresh()` swallows every exception the caller saw a plain
  `False` — indistinguishable from being offline. Percent-encoded, the same query returns 1000 items
  → 95 mapped models with a real `_updated`. The TypeScript twin was never affected (`fetch` encodes
  for us). A test now asserts every built-in source URL is one `urllib` will accept, with a negative
  control on the exact shape that shipped.

- **A redacted Gemini call is sendable again: `Reroute(messages=…)` maps back to Gemini's `contents`
  shape.** `_extract_request` normalizes a **non-list** `contents` — the very common
  `generate_content(contents="summarize…")` — into one canonical `{role, content}` message, so every
  interceptor sees every provider the same way. `_apply_reroute` then wrote that message object
  straight back onto `contents`, and google-genai rejects it: `contents` takes a string, a `Content`
  (`{role, parts}`) or a `Part`, never `{role, content}`. So `cendor-acttrace`'s `guard()`
  redact-before-send scrubbed the payload correctly and then made the call impossible to send — the
  redaction fired, the audit entry chained, and the request raised.

  The back-map mirrors the one `openai_embeddings` already had: **the original request's shape is
  what goes back.** A string input that produced a single text message returns as a string; a
  `Content`/`Part` passes through untouched (a list input is already Gemini-native, and the guard's
  deep scrub preserves its shape — pinned by its own test); a canonical message becomes
  `{role, parts: [{text}]}`, with `assistant`/`model` mapped to Gemini's `model` role.

  Only the reroute path changes — a call with no interceptor rewrite, and every other provider, is
  byte-identical to before. Found by the external black-box suite driving a live Gemini key
  (reported against `cendor-acttrace`; the fix belongs here, because `guard()`'s scrub is a
  shape-preserving structural walk and was never the problem). Mirrors the `@cendor/core` fix.

### Not changed (investigated, did not reproduce)
- An external report that **reasoning tokens are not captured for `o3-mini`** does not reproduce.
  `_extract_usage` reads `completion_tokens_details.reasoning_tokens` (and the Responses API's
  `output_tokens_details.reasoning_tokens`) for the whole OpenAI family with **no model-name test
  anywhere**, and `o3-mini` is in the bundled price snapshot, so the call is priced. Verified against
  the real `openai` SDK's response objects on both entrypoints. A parametrized regression guard
  across `o3-mini`/`o1-mini`/`o3`/`o4-mini`/`gpt-5.4` now pins the model-name independence; no
  behaviour was changed and no pricing model was invented.

## [1.14.2] — 2026-07-27
**The raw-response family, part two — the edges 1.14.1 left open.** Every case below was measured
against the real `openai` SDK before it was fixed; the follow-up evidence pack is
`plan/evidence-ripple-followup-2026-07-27/` in the workspace. No public API is added.

### Fixed
- **`chat.completions.parse` is captured.** The Chat Completions structured-output entrypoint was
  not an instrumented target, so it emitted nothing at all — no budget, no guard, no audit, no
  cassette. This is not a symmetry nicety: `langchain-openai` takes that exact branch on **every**
  `with_structured_output()` call over Chat Completions (`chat_models/base.py`, the
  `if "response_format" in payload` branch), so the most common structured-output idiom in the most
  popular Python framework was invisible. Like `responses.parse` it POSTs its own request rather
  than delegating to `create`, so wrapping both cannot double-count. `callable()`-gated for older SDKs.
- **A raw-response call with `stream=True` no longer hands back a broken stream.**
  `client.chat.completions.with_raw_response.create(..., stream=True)` — what `langchain-openai`
  does when it streams — returns an *envelope*, not a stream. Core wrapped it as a stream anyway:
  iterating raised `AttributeError: 'LegacyAPIResponse' object has no attribute '__aiter__'` **from
  inside cendor**, and the working path (`envelope.parse()`) bypassed the proxy, so nothing was
  counted. The envelope is now handed back untouched, with `parse()` memoized and wrapped, so
  consuming the stream emits exactly one `LLMCall` with usage.
- **A raw-response accessor resolved before `instrument()` no longer silences the call.** openai
  builds `with_raw_response` / `with_streaming_response` as `cached_property`; an app that reached
  for one first froze a wrapper around the **un-instrumented** method, and every call through it was
  invisible — zero events, no error. `instrument()` now evicts those cached entries so the next
  access rebuilds them around the wrapped method. **Honest limit:** a reference the caller already
  stored in a local is beyond reach; wrap the client at construction.

### Changed
- `LLMCall.metadata["response_body"]` now carries the decoded payload whenever the captured value
  was a raw-response envelope. `metadata["response"]` still holds exactly what the SDK returned;
  recorders should prefer the body, because walking an envelope's object graph sent
  `cendor-cassette`'s serializer to the recursion limit (fixed in `cendor-cassette` 1.1.1).

## [1.14.1] — 2026-07-27
**A replayed call keeps its `await`, and a raw-response envelope keeps its cost.**

Three capture repairs, all found by driving a real Microsoft 365 pro-code agent against the published
shelf. Nothing is added to the public API.

### Fixed
- **A replayed call on an async client is awaitable again.** `openai`'s `chat.completions.create` and
  `anthropic`'s `messages.create` are `async def`s behind a **sync** `functools.wraps` decorator, so
  `inspect.iscoroutinefunction()` is `False` and core installs its sync wrapper. The live path already
  repaired that; the *interceptor short-circuit* — the seam `cendor-cassette` replays through — handed
  the recorded value back synchronously, so an ordinary `await client.chat.completions.create(...)`
  raised `TypeError: object … can't be used in 'await' expression` under `cassette.using(…,
  mode="replay")`. A **streamed** replay was worse: a *sync* proxy, so neither `await` nor `async for`
  worked, which no app-side await-shim could paper over. The short-circuit now honours the wrapped
  method's async contract (inferred once at wrap time via `inspect.unwrap`, and confirmed by the live
  path's own observation, so record-then-replay works for a hand-written client too). A decorated
  *sync* client is untouched, and a pre-flight refusal still raises in the caller's frame.
- **Usage and cost survive a raw-response envelope.** A call made through
  `client.responses.with_raw_response.create(...)` — the documented way to read response headers, and
  what Microsoft Agent Framework 1.12.1 drives OpenAI through — returns an envelope (headers plus the
  un-parsed body) carrying no `usage` of its own, so the call was captured with `usage=None` and
  `cost=None` while the identical `responses.create` call priced exactly. Usage is now recovered from
  the buffered body (duck-typed, no SDK import) and the entry is marked
  `metadata["raw_response_envelope"]`. Strictly additive: the fallback runs only when direct
  extraction found nothing, and an unread streaming body degrades to the previous `None` rather than
  raising.
- **`responses.parse` is captured.** The Responses structured-output entrypoint issues its own request
  rather than delegating to `create`, so a structured-output call emitted **no event at all** (the
  branch MAF takes whenever a `text_format` is set). It is now an instrumented target with the same
  request/response shape as `create` — exactly one `LLMCall` per call — and `callable()`-gated, so an
  older SDK without it is simply not wrapped.
- **An async tool no longer records its own coroutine.** `instrument_tool()` never received the
  async-detect repair, so a tool that is an `async def` behind a decorator (retry, cache, tracing)
  recorded `ToolCall.result = <coroutine object …>` — which a recorder then persists *as that string*,
  silently poisoning a cassette. The result is now awaited before it is recorded, and a replayed async
  tool call is awaitable.

## [1.14.0] — 2026-07-26
**`core.trace()` groups your calls into one trace, and a governance row can finally name the agent it stopped.**

### `trace()` is a real span — the behaviour change to read before upgrading

`with trace("id"):` used to stamp an ambient id onto every `LLMCall`/`ToolCall` and nothing more, so
every call inside still arrived as its **own root span**: one logical unit of work became N unrelated
traces in any backend that groups by trace. Measured against Cendor Monitor on 2026-07-26, a scope
around a chat call *and* a tool call produced **two** traces sharing one id — one run, two rows, no
parent, its governance fanned out to both, and per-run governance counts doubled.

The scope now brackets its calls with a `cendor.trace <id>` span (instrumentation scope `cendor.core`,
carrying `cendor.run.id` and `cendor.scope="trace"`), so **one scope is one trace**, and each child
call carries a 1-based `cendor.step`. The ambient id is stamped exactly as before, so correlation by
`cendor.trace_id` is unaffected.

Nothing is emitted when there is nobody to emit to (no OpenTelemetry, no configured provider, or
`CENDOR_TELEMETRY=off`), and **no span is opened inside a cendor-sdk run** — that run already owns its
trace, so the calls attach to it rather than to a competing root. Nesting is a no-op for the inner
scope: one root per scope family.

**If your backend groups by trace id today and you want the old shape**, one switch restores it:
`CENDOR_TRACE_SPAN=off`, or `trace(id, span=False)` for a single scope.

### `ambient_attrs()` — so a governance record can name its actor

`apply_ambient` covers everything that *is* an event. A governance record is not: an audit entry or an
enforcement decision is built by `acttrace` / `tokenguard` / `guardrails`, which must not import the
SDK, and so had no way to learn which agent was acting. Measured: **13 of 386** governance rows named
their agent. `ambient_attrs()` is a **read** of the same registry — core still carries no identity of
its own — and core's own `governance.*` spans now use it, so a **budget block** (an event with no agent
field at all) stops being an anonymous row. An agent name is app-supplied configuration, not
input-derived text, so it does not breach the rule that keeps a guardrail `reason` off these spans.

### Provider-native agent identity, in the adapters

* `gen_ai.agent.id` is emitted on a call span whenever something stamped one — **never** hashed and
  never a placeholder. A name is a label (two apps can share one, and a rename loses that agent's
  history); an id is identity.
* **New `cendor.core.agent_ids`**: `bedrock_agent_scope(agent_id=…, agent_alias_id=…, session_id=…)`,
  `openai_assistant_scope(assistant_id=…, thread_id=…)` and the generic `agent_scope(...)`, mapping the
  ids those products already own onto `gen_ai.agent.id` / `gen_ai.conversation.id`.
* `cendor.core.foundry` now also maps its `agent_id` onto `gen_ai.agent.id` (it keeps stamping `agent`,
  so a dashboard grouping on the name dimension does not lose its rows).
* All three stay **attribution-only**: mapping identity does not make a server-side runtime's tokens or
  cost appear, and the docs say so.

## [1.13.0] — 2026-07-25
**Governance is now visible as ordinary telemetry — with no audit object and no `audit.*` vocabulary.**

Until now the only way a budget block or a guardrail verdict reached your backend was the *audit
mirror*, so seeing enforcement meant adopting the evidence library. Under the telemetry switch, the
decisions your stack makes are rendered as plain monitoring spans:

| Span | Attributes |
|---|---|
| `governance.budget_event` | `cendor.gov.type/action/budget/scope/model/to_model/projected_usd/cap_usd/projected_tokens/cap_tokens` + `cendor.trace_id` |
| `governance.guardrail_decision` | `cendor.gov.type/guardrail/stage/action/agent/tool` + `cendor.trace_id` |

Scope is `cendor.core` for a libs app; inside an SDK run, `cendor-sdk` renders the same events as
children of the run root (`cendor.sdk`), so the decision sits next to the steps it governed.

### Added
- The two renderings above on the bus→span emitter, duck-typed exactly like `acttrace` chains them
  (core imports no tool — rule 2).
- **`otel.governance_mirrored(on)` / `otel.governance_mirror_active()`** — `acttrace` refcounts a
  mirror that emits spans, and while one is live these ops renderings **stand down**: the chained
  `audit.*` spans are richer and must win, and an event must never render twice. A *custom* mirror
  (a SIEM sink) deliberately does not suppress them — nothing audit-shaped is on the wire then.

### Rule 6 (honesty), by construction
No `audit.*` span name, no `cendor.audit.*` attribute, nothing evidence-shaped: "audit" keeps meaning
the hash-chained file `verify()` checks. And **no `reason` string is emitted** — a guardrail's reason is
written by the rule, and by a judge *model* for `rules.llm_judge` (free text that can paraphrase the
payload; the URL rules embed the matched host). The audit chain — an artifact you declared — keeps
carrying it; these default-on spans do not. A test pins that no payload marker can reach a
`cendor.gov.*` attribute.

## [1.12.0] — 2026-07-25
**Telemetry now flows with zero telemetry code — and `CENDOR_TELEMETRY=off` turns it all off.**

⚠️ **This is a default-behaviour change.** If OpenTelemetry is installed (`pip install
"cendor-core[otel]"`) **and** your app configures a global tracer provider (`configure_azure_monitor()`,
a plain `set_tracer_provider`, an OTLP endpoint pointed at Cendor Monitor…), then after upgrading a
governed call arrives in **your** backend as a standard `gen_ai.*` span without a line of Cendor
telemetry code. You will see:

| What appears | From | Scope / names |
|---|---|---|
| `chat …` / `execute_tool …` span per governed call | this package — the emitter attaches itself at your first `instrument()` (or `otel.ingest()`) | `cendor.core`, standard `gen_ai.*` |

(`cendor-tokenguard` 1.6.0 adds the spend counters, `cendor-acttrace` 1.11.0 the `audit.*` mirror,
`cendor-sdk` 1.18.0 the run root — same switch, same default.)

Cendor still has **no endpoint, no exporter and no collector of its own**: it emits into the provider
*you* configured. With OpenTelemetry absent, or with no provider configured, behaviour is
byte-identical to 1.11.x — not one extra bus subscriber. Prompt/response **content stays opt-in**
(`otel.capture_content()`). No new identity: the app name is still the OTel resource's `service.name`.

**Turning it off / diagnosing it**
- `CENDOR_TELEMETRY=off` — process-wide, no code change; read per event, so it applies even if you
  export it late. `OTEL_SDK_DISABLED=true` (the standard switch) composes for free.
- `CENDOR_DEBUG_TELEMETRY=1` — one stderr line stating the mode, whether a provider was detected and
  what got wired. Silent otherwise: Cendor never nags an offline app.

### Added
- **`otel.telemetry_mode()`** — the effective mode from `CENDOR_TELEMETRY` (`"auto"` default | `"off"`;
  an unrecognised value is `auto`, noted once under `CENDOR_DEBUG_TELEMETRY=1`, because a typo must
  never silently disable telemetry).
- **`otel.provider_configured()`** — True once the app registered a real (non-proxy) global tracer
  provider. It never inspects exporters or endpoints.
- **`otel.live_spans_active()`** — whether an SDK `live_spans` scope is open in this context (the SDK
  reads it so an explicit scope always wins over its automatic one).
- **`otel.auto_telemetry_state()`** — a diagnostics dict (`mode`/`otel`/`provider`/`armed`/`emitting`/
  `manual`), for `cendor-init doctor` and tests.
- **Automatic span emitter.** `instrument()` / `otel.ingest()` arm **one** bus subscriber that stays
  dormant — re-checking the ~300 ns provider predicate per event — until a provider appears, then
  latches and renders. Attach order therefore never matters, and a provider configured *after* the
  first call is still caught. `use_span_emitter()` still works and **always wins**: a manual
  attachment detaches the automatic one, so an event is never rendered twice.

### Fixed
- **`use_span_emitter(tracer)` now honours an explicitly passed tracer when OpenTelemetry is not
  installed.** The `ImportError` guard ran first, so passing your own tracer (or a recording double)
  into an OTel-less environment silently subscribed nothing. The TypeScript port never had this
  asymmetry.

## [1.11.1] — 2026-07-24
**Fix: the openai-agents adapter now actually stamps the agent name on live calls.** Found by the black-box testsuits live probe: the OpenAI Agents SDK runs each model call in an async context **isolated** from the `RunHooks`, so the `ContextVar` set in `on_agent_start` (or `on_llm_start`) never reached the captured `LLMCall` — the name was silently dropped live (the offline fixture passed because it drove hooks + call in one context). `instrument()` *did* capture the call with real usage, so "the calls ride the standard client" always held; only the name was missing.

### Fixed
- **`cendor.core.openai_agents`** now tracks the active agent in a **process-wide holder** (updated by the hooks, read live at event construction) instead of a `ContextVar` — so the framework's agent name reaches the model call for real (verified live). **Honest limit:** correct for sequential runs + handoffs (the common case); concurrent `Runner.run()` in the same process may cross-attribute during overlap (per-run scoping is impossible — the SDK isolates the call's context from the hooks). Run concurrent multi-agent workloads in separate processes. `cendor.core.foundry` is unaffected (its `foundry_agent_scope` is a synchronous callback wrap — the scope *is* the call's context).

## [1.11.0] — 2026-07-23
**Framework agent-name adapters** — two optional integrations that source a *third-party framework's* agent identity onto the bus, so the monitor's Agents page fills for framework-driven stacks. Additive; nothing changes unless you attach one (importing an adapter registers no ambient provider — core's zero-provider fast path is untouched). Core carries no identity of its own (Raghav's locked principle) — the framework owns the name; these adapters carry it.

### Added
- **`cendor.core.openai_agents.CendorAgentHooks`** (extra `[openai-agents]`) — a `RunHooks` you pass to the OpenAI Agents SDK's `Runner.run(..., hooks=…)`. On each agent turn it stamps the framework's agent name via a scoped ambient provider (set at agent start / handoff, cleared at end); the agent's model calls ride the standard OpenAI client, so `instrument()` still captures tokens/cost/streaming — this supplies *only* the name (GLR-11c). Mirrors the `cendor.core.langchain` handler; never-overwrite.
- **`cendor.core.foundry`** (`observe_foundry_agents(client)` + `foundry_agent_scope(agent_id, thread_id)`; extra `[foundry]`) — a correlation adapter for Azure AI Foundry Agents. It wraps `client.runs.{create,create_and_process,stream}` (duck-typed on `.runs`, sync + async) to stamp `agent` + `conversation_id` for the run's duration. **Attribution only** — the model runs server-side, so there is no per-step token/cost capture here (a documented honest limit). Importing this module needs no Azure SDK (it wraps a client you pass in).

## [1.10.0] — 2026-07-23
The per-chunk **stream-observer seam** + visible-thinking stream estimation + two Python capture repairs. Additive; nothing changes unless an observer is registered.

### Added
- **`add_stream_observer(fn)` / `remove_stream_observer(fn)`** — register a per-chunk observer `fn(call, delta_text, delta_thinking)` on every instrumented stream. **Raising aborts the stream** (interceptor discipline): core closes the underlying provider stream, finalizes the `LLMCall` once with the partial (estimated) usage, and re-raises to the consumer. Zero observers ⇒ one truthiness/length check per chunk (streaming hot path untouched). This is the generic seam `cendor-tokenguard`'s mid-stream budget breaker (`budget(on_exceed="break")`) rides — core learns no budget vocabulary.
- **Bedrock `converse_stream` capture (Python)** — a Bedrock client's `converse_stream` (no `stream=` kwarg; the event iterable arrives as the `"stream"` member of a dict response) is now wrapped, priced, and recorded like any stream.

### Changed
- **Streamed usage estimation now counts *visible* thinking** — Anthropic `thinking_delta`, Ollama `message.thinking`, OpenAI-compat `reasoning_content`, and Bedrock `reasoningContent` are folded into output and surfaced as reasoning. Narrows the documented limit from "can't see thinking" to "can't see *hidden* thinking" (OpenAI-native/Gemini reasoning still never reaches the wire).
- **HF streamed-usage injection is signature-gated** — `stream_options={"include_usage": True}` is injected for Hugging Face only when the installed `huggingface_hub`'s `chat_completion` explicitly accepts it (never blind — avoids a 4xx / `TypeError` on older hubs). OpenAI is unchanged.

### Fixed
- **Async-detect repair (Python).** A sync-looking client method that actually returns an awaitable (a misdetected async client — `iscoroutinefunction()` was `False`) now has its usage captured via an awaited continuation, instead of silently losing usage on the un-awaited coroutine. A truly sync client never returns an awaitable, so there are zero false positives.

## [1.9.0] — 2026-07-22
The ambient metadata seam — the one core-owned pre-emit capture point for run context. Additive; nothing changes unless a provider is registered.

### Added
- **`add_ambient_provider(fn)` / `remove_ambient_provider(fn)`** — register a `(event) -> dict | None` provider that runs at every event's construction (the caller's synchronous frame, before interceptors), merging its metadata onto `event.metadata` with never-raise / never-overwrite / registration-order semantics and a zero-provider single-length-check fast path. This is how a library (or app) attaches agent / conversation id / budget frames / decision id / cassette session at the moment it is unconditionally correct, instead of re-reading contextvars at bus-delivery time (which breaks for streams finalized outside their scope, context-losing layers, subscriber order, concurrent runs, and Python generators that leak run scopes into the consumer).
- **`otel.ingest()` stamps the ambient `trace_id`** at construction, so an ingested call joins its run.
- **`otel.use_span_emitter()` maps `metadata["agent"]` → `gen_ai.agent.name`** — a libs-only app self-identifies an agent (via a provider or the LangChain handler) with no SDK.
- **The LangChain callback handler stamps an agent/chain/LangGraph-node name** into `metadata["agent"]` (explicit `metadata["agent"]` wins).

## [1.8.0] — 2026-07-21
Estimated-usage provenance on emitted spans — the emission-truth half of Monitor v5 (G-V4-3). Additive; nothing changes unless a streamed call's token count was recovered by offline estimate.

### Added
- **`cendor.usage_estimated="true"`** on an emitted `chat` span (the libs-only `otel.use_span_emitter()`) when the streamed call reported no usage and the count was recovered by `_estimate_stream_usage` (`metadata["usage_estimated"]`). Truth = the product: a monitor can now render those tokens as *est.* rather than the provider's billed figure. Stamped only when set (a real, provider-reported count leaves the span unflagged).

## [1.7.0] — 2026-07-20
Opt-in content capture, a libs-only span emitter, and TTFT — the emission half of the Cendor journey console (Monitor v3).

### Added
- **Opt-in content capture (OFF by default)** — `otel.capture_content(mode="span", mask=…, max_bytes=…)` and the standard `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` env var turn on export of prompts/responses/thinking/tool values onto the semconv content span attributes (`gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.system_instructions`, JSON strings). A `mask` (`messages -> messages`) scrubs before export (fail-closed if it raises); `max_bytes` caps each attribute with a truncation marker. **Content never enters the acttrace evidence chain or its mirror** (rule 6). Helpers `otel.content_attrs(...)` / `otel.tool_content_attrs(...)` build the attribute dicts.
- **`otel.response_messages(call)`** — best-effort per-provider parse of assistant output into text + **thinking** parts (Anthropic `thinking` blocks, OpenAI Responses reasoning summaries, Gemini `thought` parts, Bedrock `reasoningContent`, Ollama `thinking`, DeepSeek `reasoning_content`) — the content the provider `parse()` drops. Content only, opt-in.
- **`otel.use_span_emitter()`** — an opt-in bus→span subscriber that emits a `chat`/`execute_tool` semconv span per `LLMCall`/`ToolCall`, so a **libs-only app (no SDK)** lights up a trace-based monitor. Honors content capture; defers to an active SDK `live_spans` context (no double spans) via `otel.enter_live_spans()`/`exit_live_spans()`.
- **TTFT** — streamed calls now stamp `metadata["ttft_ms"]` (first-chunk latency), surfaced as `cendor.ttft_ms` on emitted chat spans. Replayed streams are excluded.

## [1.6.0] — 2026-07-14
Embeddings capture, Usage arithmetic, and a survive-refresh price registry — the core half of the SDK↔lib inheritance fixes.

### Added
- **`instrument()` now captures `embeddings.create`** on openai-shaped clients (OpenAI + Azure-via-openai): the pre-flight interceptor pass runs — budget block/clamp and guard redact-before-send now apply to embedding calls (a `Reroute(messages=…)` maps back to the raw `input` shape) — and the emitted `LLMCall` carries `metadata["embedding"] = True`, usage from `response.usage`, and cost from the price table. Sync + async. Embeddings leave the documented capture-gaps list for openai-shaped clients.
- **`Usage` arithmetic** — `Usage.__add__` (supports `sum(...)`) and `sum_usage(iterable)`, exported from `cendor.core`. Field-complete **by construction** (iterates the dataclass fields), so a future `Usage` field can never silently vanish from an aggregate.
- **`prices._register(model, rates)`** — the contractual programmatic write hook (the seam `cendor.sdk.register_model_price` writes through; underscore-named to stay out of the end-user API, but stable within 1.x). Registrations **survive `refresh()`** — re-applied after every table swap instead of being dropped.
- The bundled price snapshot gains the OpenAI embedding rows (`text-embedding-3-small` $0.02/1M · `text-embedding-3-large` $0.13/1M · `text-embedding-ada-002` $0.10/1M — verified on the official model pages), so USD budgets bind on embedding calls out of the box.

## [1.5.2] — 2026-07-11
Model-currency patch: today's models price correctly out of the box.

### Changed
- **Price snapshot regenerated for the current model generation** (`_updated` 2026-07-11; every
  rate verified against the official provider pricing pages): adds the OpenAI gpt-5.x line
  (5.6-sol/terra/luna, 5.5, 5.5-pro, the 5.4 family, 5.3-codex, 5.2, 5.1), Anthropic
  claude-fable-5 / claude-mythos-5 / claude-sonnet-5 (listed at the standard rate effective
  2026-09-01; the intro rate through 2026-08-31 is noted in `_note`) / opus-4-7/-4-6/-4-5,
  Gemini 3.x (3.5-flash, 3.1-pro-preview, 3.1-flash-lite, 3-flash-preview), and xAI grok-4.3 /
  grok-4.5. **claude-haiku-4-5 corrected** to the official $1/$5 (+ $0.10 cache read / $1.25 5m
  write) — the old row carried Haiku 3.5 rates. Gemini 2.5 cache-read rates updated
  ($0.125 / $0.03). Dead rows removed: gemini-2.0-flash (shut down 2026-06-01), gemini-1.5-pro.
  gpt-4o / gpt-4.1 / o-series stay as legacy rows.
- **Wire-level model ids now price at lookup**: Bedrock modelIds (`anthropic.…-v1:0`,
  `us.`-region profiles) and dated Anthropic / OpenAI snapshot ids resolve to their base row
  instead of yielding `cost=None`. Unknown models still raise `UnknownModelError` — normalization
  never invents a price.

### Docs
- The token-exactness claim is scoped honestly: `tiktoken` (0.13.0) ships no gpt-5.x mapping yet,
  so gpt-5.x counts via the `o200k` BPE proxy (`method()` reports `bpe-estimate` and upgrades
  automatically when a mapping ships). New honest-limits entry + trap-table row for the
  entrypoints `instrument()` does not capture (`chat.completions.parse` / `responses.parse`,
  Anthropic's `messages.stream()` helper + `tool_runner`, Batch APIs, embeddings).

## [1.5.1] — 2026-07-11
AI-assistant onboarding: inline Type Teach ships inside the package, plus the bundled integration guide. No runtime behavior change for correct code.

### Added
- Inline `@example` + a correct-shape signature on public symbols, so your editor's language server (and agent-mode assistants) is handed the right call as you type — the wrong shape becomes a type error whose message states the right one.
- `INTEGRATION.md` is now bundled in the installed package — a one-screen "call Cendor correctly" guide. Full trap sheet: https://cendor.ai/docs/for-ai-assistants

## [1.5.0] — 2026-07-10
Deep-QA fixes: token accuracy for the open/hosted-model class, and honest top-level exports.

### Changed
- **Non-OpenAI / unrecognized models now count via the `o200k` BPE proxy, not the character heuristic.** Any model whose family resolves to `default` — llama, mistral, deepseek, qwen, new o-series ids (`o5-mini`), and OpenAI fine-tunes (`ft:gpt-4o:*`) — routes through tiktoken's `o200k_base` estimate (reported as `bpe-estimate`), exactly like Claude/Gemini, instead of the rough char heuristic. **This changes token counts** for the whole open/hosted-model class — hence a minor — and every `tokenguard` budget / `clamp` that calls `tokens.count` inherits the correction. The o-series match is generalized (`^o\d`, so new ids don't fall through) and an `ft:` fine-tune strips to its base model, counting `exact`. The character heuristic is now only ever reached if tiktoken fails to import.

### Added
- **`add_interceptor`, `remove_interceptor`, and `MISS` are re-exported from `cendor.core`** (top-level), matching `core.md` and `@cendor/core`'s top-level exports — no more importing from the private `cendor.core.instrument`.

## [1.4.0] — 2026-07-08
### Changed
- **`tiktoken` is now a required dependency** (was the optional `[tiktoken]` extra), so a plain
  `pip install cendor-core` counts OpenAI tokens **exactly** out of the box — and therefore reports
  truthful cost/budget numbers by default. Truthful token counts are the product, not an opt-in; this
  brings Python in line with `@cendor/core`, which already hard-deps `js-tiktoken`. The character/
  subword heuristic remains in the code **only as a defensive fallback** if `tiktoken` ever fails to
  import (a broken/partial install) — it is no longer the path a normal install silently lands on.
  `tiktoken` is fully offline (no network, no account), so this preserves the local-first guarantee.
  The `[tiktoken]` extra is kept as a back-compat no-op so existing `cendor-core[tiktoken]` pins keep
  resolving. No API change: `tokens.count`/`method`/`register` are unchanged.

## [1.3.1] — 2026-07-05
### Changed
- Repository moved to `github.com/cendorhq/cendor-libs`; `[project.urls]` and the offline price-snapshot refresh URL (`prices.SNAPSHOT_URL`) now point at the new location. No API or behavior change.

## [1.3.0] — 2026-07-05
### Added
- **Hugging Face detection in `instrument()`** — a `huggingface_hub.InferenceClient` is now recognized by its `chat_completion` method and wrapped, emitting an `LLMCall` attributed to `huggingface` with usage/cost captured. The response is OpenAI-shaped, so usage extraction and streamed-text handling reuse the OpenAI path. Purely additive and backward-compatible: clients without a `chat_completion` method are unaffected, and the check runs *before* the OpenAI-compat detection so an `InferenceClient` that also exposes `chat.completions.create` is still attributed to `huggingface` (not `openai`). Enables `cendor-sdk`'s HuggingFace provider to capture governed usage/cost/audit.

## [1.2.0] — 2026-07-05
### Added
- **`cendor.core.langchain.CendorCallbackHandler`** — an optional LangChain/LangGraph callback handler (the SDK-aligned way to observe a framework) that records **usage + reasoning + cached** tokens (from LangChain's `usage_metadata`), prices each call offline, emits normalized `LLMCall`/`ToolCall` on the bus, and correlates a whole `agent.invoke` — across its nodes, react loop, and tools — under one **root-run `trace_id`**. **No client touch**, so it sidesteps the `with_raw_response` usage loss and the streaming context-manager crash. **Recording-only** (post-call): enforcement stays on the `instrument()` seam. Gated by a new optional extra `cendor-core[langchain]` (`langchain-core>=0.3`); importing the module without it raises a clear `ImportError`. Keeps core dependency-light — nothing new is a hard dependency.
- **`trace()` / `current_trace_id()` correlation hook** — an ambient `contextvars` `trace_id` stamped onto every `LLMCall`/`ToolCall` emitted inside a `with trace("run-id"):` block, so **direct-SDK** agents get the same run correlation the LangChain callback path derives from `parent_run_id`. Default is `""` (no behaviour change) unless set. A hook, not an orchestrator.
- **`Sink` protocol gained optional `flush()` / `close()` lifecycle methods.** `write(entry)` remains the **only required** member (so `runtime_checkable` still matches write-only sinks); a sink *may* additionally implement `flush()` (block until buffered records are durable) and `close()` (flush + release resources), which callers invoke via `hasattr`/`getattr` guards. This is the seam `tokenguard.sinks.QueueSink` uses to move durable I/O off the hot path. Purely additive.
- **Streamed responses are now a context manager *and* an iterator.** `instrument()`'s streaming proxy (sync and async) supports both `for chunk in stream` / `async for` **and** `with client…create(stream=True) as stream:` / `async with`, matching the provider SDK's own stream surface. This fixes a `TypeError: 'generator' object does not support the context manager protocol` crash when a framework (e.g. `langchain_openai`) consumes a streamed completion via `with`. The `LLMCall` still finalizes exactly once (on exhaustion, early `close()`, or block exit), unknown attributes (`.response`, `.close()`, …) forward to the underlying SDK stream, and replayed streams (`cassette`) gained the same surface. Additive and backward-compatible — existing `list(stream)` / `async for` iteration is unchanged.

## [1.1.0] — 2026-07-04
### Added
- **`Reroute(messages=…)`** — an interceptor can now rewrite the outbound **messages** (not just the model), mapped to each provider's own kwarg (`messages` / `input` / `contents`) and reflected on `call.messages`. Applies to sync, async, and streaming calls. This is the seam `acttrace`'s `guard()` uses for redact-before-send. Additive and backward-compatible — existing `Reroute(model=…)` / `Reroute(**kwargs)` behaviour is unchanged.

## [1.0.0] — 2026-07-03
### Added
- First release of `cendor-core` — the shared foundation for the Cendor stack: canonical types, provider-aware token counting, an offline price table, a single `instrument()` interception seam, an in-process event bus, and OpenTelemetry GenAI emitters. Kept tiny on purpose; it's the blast radius for every other tool.
- **`instrument()`** wraps any client once — OpenAI (Chat Completions **and** the Responses API), Anthropic, AWS Bedrock, Google Gemini (`google-genai` and legacy `google-generativeai`), and Ollama — detected by *shape*; sync, async, and streaming; idempotent and additive. `instrument_tool()` does the same for tools.
- **Event bus** — `subscribe` / `emit`, thread-safe within a process, where one failing subscriber never starves another.
- **Interceptor seam** — `add_interceptor` with `Reroute` / `MISS`, powering replay (cassette) and reroute/block (tokenguard) without a second patch point.
- **Token counting, three tiers** — exact (`[tiktoken]`), an o200k BPE estimate (Claude/Gemini), or an offline heuristic; `tokens.method(model)` reports which path is active and `tokens.register()` plugs in a precise counter.
- **Reasoning-token accounting** — `Usage.reasoning_tokens` breaks out a thinking model's internal reasoning (a subset of `output_tokens`, so cost is unchanged), non-streaming and streaming.
- **Offline-first, refreshable prices** — a bundled dated snapshot; `estimate() -> Decimal` money (never `float`); optional `refresh(source="litellm"|"openrouter"|"azure")` from live no-auth sources, with `age_days()` / `is_stale()` staleness signals. Cached tokens are billed once, and a gateway-reported cost is preferred over the estimate (`cost_reported` vs `cost_estimated`).
- **OpenTelemetry** — emit `gen_ai.*` spans, or `otel.ingest()` a managed runtime's spans onto the bus.
- **Structural protocols** — `Compressor`, `EvictionStrategy`, `Sink`, `Subscriber`, and `Handle` let the tools interlock without coupling.
