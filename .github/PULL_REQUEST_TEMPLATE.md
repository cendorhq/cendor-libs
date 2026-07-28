<!-- Thanks for the PR. Keep it focused and green. Full contract: CONTRIBUTING.md -->

## What & why

<!-- What does this change, and what problem does it solve? Link the related issue. Explain the *why* —
     that is the part a reviewer cannot reconstruct from the diff. -->

Affected package(s): <!-- e.g. packages/cendor-tokenguard -->

## Gates — run each one bare and read its exit code

<!-- Exactly what CI runs (.github/workflows/ci.yml). Never pipe a gate into `tail`/`grep` and chain
     the next step off `&&`: a pipeline's exit code is the last command's, so a failing check reads
     as a pass. -->

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy -p cendor.core -p cendor.tokenguard -p cendor.contextkit \
            -p cendor.squeeze -p cendor.cassette -p cendor.acttrace -p cendor.guardrails
uv run pytest -q
find packages -path '*/src/cendor/__init__.py' -print   # the namespace guard — must print NOTHING
```

- [ ] `uv lock --check`
- [ ] `uv run ruff check .` and `uv run ruff format --check .`
- [ ] `uv run mypy …` (the full `-p` list above)
- [ ] `uv run pytest -q` — green, and **offline**: no API key, no network, mocked clients or a recorded `cassette` fixture
- [ ] The namespace guard printed nothing

## Checklist

- [ ] Tests added or updated in this PR for the new behavior
- [ ] Full type hints on every public API; Google-style docstrings; internals underscore-prefixed
- [ ] Sync **and** async both supported, wherever a model or tool call is involved
- [ ] `docs/<tool>.md` updated if behavior changed — and if this is a Python-only feature, `docs/languages.md` (the parity matrix) says so and the docs page carries the *"Python only (for now)"* TypeScript panel
- [ ] A `### Added` / `### Fixed` entry under an **Unreleased** heading in `packages/<pkg>/CHANGELOG.md`
- [ ] **No version bump** — releases are cut by a maintainer (`PUBLISHING.md`)

## The rules this repo will not bend

- [ ] I did **not** create `src/cendor/__init__.py` — `cendor` is a PEP 420 implicit namespace package, and a top-level `__init__.py` silently breaks every cross-package `cendor.*` import
- [ ] No library imports another library — cooperation goes through `cendor-core`'s bus / interceptor / protocol seams (or an optional extra)
- [ ] Money is `decimal.Decimal`, never `float` — costs, prices, and budgets end to end
- [ ] Still local-first: no required account, network, or running server; provider SDKs stay optional extras
- [ ] No agent orchestration added here (loop, handoff/supervisor, tool-schema generation, provider response normalization) — that is `cendor-sdk`
- [ ] Every number I added is reproducible from the tests or the benchmark suite, and nothing claims regulatory compliance (`acttrace` produces *evidence to support* a case)
- [ ] Commit messages are conventional-ish with a body, and carry **no `Co-Authored-By` trailer**
