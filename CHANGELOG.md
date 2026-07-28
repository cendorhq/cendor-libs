# Changelog — index

**This repository has no single changelog.** It is a monorepo of nine independently versioned
distributions, so the history lives *per package*. Use the table below to jump to the one you care
about.

Every per-package file follows [Keep a Changelog](https://keepachangelog.com) and
[Semantic Versioning](https://semver.org).

## The nine changelogs

| Package | Import | Changelog | Latest on PyPI |
|---|---|---|---|
| `cendor-core` | `cendor.core` | [`packages/cendor-core/CHANGELOG.md`](packages/cendor-core/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-core) |
| `cendor-tokenguard` | `cendor.tokenguard` | [`packages/cendor-tokenguard/CHANGELOG.md`](packages/cendor-tokenguard/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-tokenguard) |
| `cendor-contextkit` | `cendor.contextkit` | [`packages/cendor-contextkit/CHANGELOG.md`](packages/cendor-contextkit/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-contextkit) |
| `cendor-squeeze` | `cendor.squeeze` | [`packages/cendor-squeeze/CHANGELOG.md`](packages/cendor-squeeze/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-squeeze) |
| `cendor-guardrails` | `cendor.guardrails` | [`packages/cendor-guardrails/CHANGELOG.md`](packages/cendor-guardrails/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-guardrails) |
| `cendor-cassette` | `cendor.cassette` | [`packages/cendor-cassette/CHANGELOG.md`](packages/cendor-cassette/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-cassette) |
| `cendor-acttrace` | `cendor.acttrace` | [`packages/cendor-acttrace/CHANGELOG.md`](packages/cendor-acttrace/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-acttrace) |
| `cendor-libs` (umbrella — pins all seven, ships no code) | — | [`packages/cendor-libs/CHANGELOG.md`](packages/cendor-libs/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor-libs) |
| `cendor` (brand alias for the umbrella, ships no code) | — | [`packages/cendor/CHANGELOG.md`](packages/cendor/CHANGELOG.md) | ![PyPI](https://img.shields.io/pypi/v/cendor) |

The badges read the live PyPI version, so this page cannot go stale. The published set is also listed
at [cendor.ai/releases](https://cendor.ai/releases) (machine-readable at
[`/releases.json`](https://cendor.ai/releases.json)).

## How the versions work

- **Independent per package.** The seven libraries are *not* a dependency chain — each stands alone
  and they cooperate only through `cendor-core`'s event bus. So they release on their own cadence:
  `cendor-core 1.14.x` alongside `cendor-squeeze 1.1.x` is normal and correct. Only bump the packages
  whose content actually changed.
- **One shared major per language family.** All `cendor-*` packages move to a new major *together*, so
  one number tells you a set is coherent. Minors and patches stay independent.
- **A new capability is a MINOR; a fix is a PATCH.** Never patch-patch-patch-then-a-surprise-major —
  the version number has to carry information.
- **A MAJOR bump needs the maintainer's explicit approval** and is never taken autonomously.
- **Not coupled across languages.** The TypeScript port (`@cendor/*`) versions on its own; parity is
  documented in [`docs/languages.md`](docs/languages.md), never expressed as matching version numbers.

## Release tags

A push to `main` publishes nothing. A release is triggered **only** by a tag matching `*-v*.*.*`:

```
<tool>-vX.Y.Z
```

The `<tool>` segment is the package's short name — `core`, `tokenguard`, `contextkit`, `squeeze`,
`guardrails`, `cassette`, `acttrace` — plus `libs` for the umbrella and `cendor` for the brand alias
(so: `core-v1.0.0`, `libs-v1.0.0`, `cendor-v1.0.0`). That segment selects both the package to build and
its PyPI trusted-publisher environment, and the release workflow hard-guards that the tag version
equals the `pyproject.toml` version before building. Never re-tag an already-published version — PyPI
rejects a duplicate upload and the run fails.

Full runbook: [`PUBLISHING.md`](PUBLISHING.md).

## Contributing a changelog entry

Add your `### Added` / `### Changed` / `### Fixed` bullet under an *Unreleased* heading in the affected
package's own `CHANGELOG.md` — not here. Don't bump a version number in a PR; a maintainer cuts the
release. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
