# Contributing to cendor-libs

Thanks for your interest in contributing. This file covers **this repository** — the Python monorepo
that publishes the seven Cendor libraries plus the `cendor-libs` umbrella and the `cendor` alias.

## Ground rules

- **Honest claims.** Every number in docs, READMEs, or the site must be reproducible from the
  benchmark suite or the tests. Never overstate coverage, test counts, provider support, or
  compliance. `acttrace` produces *evidence to support* compliance — never a guarantee.
- **Local-first.** No library may require an account, network, or running server. Provider SDKs
  (`openai`, `anthropic`, …) are **optional extras**, never hard dependencies.
- **Small, composable, no cross-imports.** The libraries cooperate only through `cendor-core`'s event
  bus / interceptor seams — they never import one another.
- **Never create `src/cendor/__init__.py`.** `cendor` is a PEP 420 implicit namespace package; a
  top-level `__init__.py` breaks every cross-package import. This is the repo's cardinal rule.
- **Money is `Decimal`, never `float`.**
- No agent orchestration here (loops, handoff, provider response normalization) — that belongs to
  `cendor-sdk`.
- Be respectful and constructive — see the [Code of Conduct](CODE_OF_CONDUCT.md).

The full set of conventions lives in [`CLAUDE.md`](CLAUDE.md); the testing strategy is in
[`TESTING.md`](TESTING.md).

## Getting set up

A [uv](https://docs.astral.sh/uv/) workspace on Python ≥ 3.11:

```bash
uv sync                                    # create the env, install all nine packages editable
uv run pytest                              # every package
uv run pytest packages/cendor-tokenguard   # one package
```

All tests run **offline** — no API key, no network. If a change needs a network call to pass, it
doesn't belong in the test suite (use a recorded `cassette` fixture instead).

## The gates (run these before you open a PR)

Exactly what CI runs, in [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```bash
uv lock --check                            # the lockfile must already match pyproject
uv run ruff check .
uv run ruff format --check .
uv run mypy -p cendor.core -p cendor.tokenguard -p cendor.contextkit \
            -p cendor.squeeze -p cendor.cassette -p cendor.acttrace -p cendor.guardrails
uv run pytest -q
# namespace invariant — must print nothing:
find packages -path '*/src/cendor/__init__.py' -print
```

CI also builds the wheels and installs them into a clean venv on Python 3.11/3.12/3.13 (the `smoke`
job) to prove the shipped artifacts still import as one `cendor.*` namespace.

## Making a change

1. Open an issue first for anything non-trivial, so we can agree on the approach.
2. Fork, branch, and keep changes focused. Match the surrounding code's style.
3. Full type hints on every public API; Google-style docstrings on public functions and classes; keep
   the public surface small and underscore-prefix internals.
4. Support sync **and** async wherever a model or tool call is involved.
5. Add or update tests in the same PR. New behavior ships with tests — mocked clients, golden values,
   no network.
6. Update the relevant `docs/` page (each package has one: `docs/<tool>.md`). If you add a
   Python-only feature, note it in [`docs/languages.md`](docs/languages.md) so the parity matrix stays
   honest.
7. Add a `### Added` / `### Fixed` entry under an *Unreleased* heading in the affected package's
   `packages/<pkg>/CHANGELOG.md`. Do **not** bump a version number yourself — releases are cut by a
   maintainer (see [`PUBLISHING.md`](PUBLISHING.md)).
8. Open a PR against `main` with a clear description of the *why*.

Adding a whole new package is a maintainer-side task with its own scaffold (the `new-package` skill in
`.claude/skills/`) — open an issue first.

## Commit and PR conventions

- Conventional-ish commit messages (`feat:`, `fix:`, `docs:`, `chore:`), with a body explaining the
  reasoning.
- Do **not** add a `Co-Authored-By` trailer.
- Keep PRs green: CI runs lint, format, the namespace guard, type checks, tests + coverage, and the
  install smoke matrix on every push.

## Reporting a security issue

Do not open a public issue — see [`SECURITY.md`](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the project's
[Apache-2.0](LICENSE) license.
