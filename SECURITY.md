# Security Policy

We take the security of the Cendor libraries seriously. Thank you for helping keep them and their
users safe.

This policy covers the packages published from this repository: `cendor-core`, `cendor-tokenguard`,
`cendor-contextkit`, `cendor-squeeze`, `cendor-guardrails`, `cendor-cassette`, `cendor-acttrace`, the
`cendor-libs` umbrella, and the `cendor` brand alias.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report vulnerabilities privately through **GitHub Private Vulnerability Reporting**:

> https://github.com/cendorhq/cendor-libs/security/advisories/new

(or open the repository's **Security** tab and choose **Report a vulnerability**). This creates a
private advisory only the maintainers can see, and lets us collaborate on a fix and coordinate
disclosure with you.

Please include, where you can:

- the affected package(s) and version(s),
- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- any known mitigations.

## Scope

These are **local-first libraries** — they run in your process, with no Cendor-operated servers or
network services, and no account or API key of ours. That shapes the threat model: there is no hosted
endpoint to attack. Relevant classes of issues include, for example:

- redaction bypasses in `acttrace` (or in `cassette`'s recording redaction),
- audit hash-chain verification flaws, or `_meta`/HMAC signature forgery in `acttrace`,
- incorrect budget enforcement in `tokenguard` (spend that escapes a cap),
- a `guardrails` gate that can be evaded, or that fails *open* where it should fail closed,
- unsafe deserialization of a cassette, policy file, or store,
- secret leakage into a log, span, audit entry, or exception message.

Out of scope: findings that require a modified build of the library, the security of a third-party
provider SDK we merely wrap (report those upstream), and anything that depends on you deliberately
opting into content capture and then exporting it somewhere unprotected.

`acttrace` produces **evidence to support** a compliance case — it is not a compliance guarantee.

## What to expect

- We aim to acknowledge a report within a few business days.
- We'll work with you on a fix and a coordinated disclosure timeline, and credit you in the advisory
  unless you prefer to remain anonymous.

## Supported versions

Fixes land on the latest released minor of each affected package. Package versions here are
**independent per package** (see [`CHANGELOG.md`](CHANGELOG.md)), and independent across languages —
so the same fix may ship under different version numbers in Python and in the TypeScript port.
