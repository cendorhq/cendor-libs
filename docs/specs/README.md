# Cendor format specifications

These pages specify the **cross-language, cross-version data contracts** of the Cendor stack — the
formats that must interoperate no matter which language produced them. They exist so that the Python
packages in this repo are understood as *one implementation of a spec*, not the spec itself. A future
TypeScript port (`@cendor/*`) is bound by exactly these documents.

Each spec is **versioned independently** of the packages that implement it (a spec version like
`cassette/1` changes only on a wire-format change, far less often than package versions). Where a spec
version and a package feature move together, the spec says so.

| Spec | Governs | Implemented by |
|---|---|---|
| [Cassette file format](cassette-format.md) | the on-disk record/replay file | `cendor-cassette` |
| [acttrace chain](acttrace-chain.md) | the tamper-evident audit log (canonical bytes, hashing, signing, verify) | `cendor-acttrace` |
| [Price dataset](price-dataset.md) | the model price table both languages consume | `cendor-core` (`prices`) |
| [Bus event shapes](bus-events.md) | `LLMCall` / `ToolCall` / `Usage` / `Money` — the shared vocabulary | `cendor-core` (`types`, bus) |
| [API parity rules](api-parity.md) | how the public API maps mechanically across languages | all packages + `cendor-sdk` |

## Why these are contracts, not just docs

Two rules make interoperability real rather than aspirational:

1. **A payload written by one language must be consumed by another.** A cassette recorded by Python
   replays in JavaScript; a chain written in JavaScript `verify()`s in Python. This only holds if the
   *bytes* — not just the field names — are specified: JSON canonicalization, number formatting,
   Unicode handling, and hashing inputs are all pinned below, not left to each implementation.
2. **Money is never an IEEE float, in any language.** Costs are exact decimals serialized as strings.
   A reimplementation that uses binary floating point is non-conforming even if it "usually" agrees.

## Conformance vectors

Golden fixtures — recorded cassettes, signed chains, a price snapshot, canonical bus events — are the
executable form of these specs. Every language implementation's CI must pass them; a change here that
would break another language must ship updated vectors in the same change. (The vector set is assembled
alongside these specs.)

## Versioning & stability

- A spec's version is the integer embedded in the artifact (e.g. a cassette's `version` field). Adding
  an optional field that older readers can ignore is a **compatible** change and does not bump it;
  changing the meaning, hashing input, or requiredness of a field is a **breaking** change and does.
- These formats are **local-first**: no spec requires a network service to read, write, or verify.
