# CLAUDE.md — cendor

Project **constitution**. Always in effect. Task playbooks → `SKILLS.md` and `.claude/skills/`. Deep design → `docs/`. Testing strategy → `TESTING.md`. Release runbook → `PUBLISHING.md`. Per-package history → `CHANGELOG.md` (an index over `packages/*/CHANGELOG.md`). Contributing, security reporting, and conduct → `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`. (Maintainers also keep an uncommitted local scratch file for in-flight decisions; nothing in this repo depends on it.)

## What this is
Cendor — *"Production plumbing for LLM applications."* A monorepo publishing a family of small, composable Python libraries that sit **beneath** agent frameworks: context, cost, testing, governance. Framework-agnostic. Local-first. Apache-2.0.

Lifecycle of one governed call: `contextkit` (assemble) → `squeeze` (compress) → `tokenguard` (budget) → `guardrails` (gate) → `cassette` (test) → `acttrace` (guard + audit), on a shared foundation `cendor-core`. Seven libraries, not a dependency chain — each stands alone and they cooperate only on core's bus. All under the `cendor.*` import namespace.

## Cardinal rules — DO NOT BREAK
1. **NEVER create `src/cendor/__init__.py`.** `cendor` is a PEP 420 implicit namespace package. A top-level `__init__.py` breaks every cross-package import. Each package owns only `src/cendor/<tool>/` (which *does* have its own `__init__.py`).
2. **Tools never import each other.** They cooperate only through `cendor-core` (shared types/protocols + event bus) or an optional extra (e.g. `contextkit[squeeze]`). No tool→tool hard dependency.
3. **Keep `cendor-core` tiny and stable.** It is the blast radius for the whole stack. Add to it only what a tool needs, when it needs it. Never big-design core up front.
4. **Local-first, no servers.** No library may require an account, network, or running infrastructure. Cloud and OpenTelemetry export are always optional.
5. **Don't ship empty packages.** A package reaches PyPI only with tests + a README + a real `v0`.
6. **`acttrace` produces *evidence*, not a guarantee.** Never claim "EU AI Act compliant" — it provides evidence to *support* compliance.
7. **Agent orchestration lives in `cendorhq/cendor-sdk`, not here.** Never add an agent loop, handoff/supervisor, tool-schema generation, or provider response-normalization to any library in this repo. SDK needs land in `core` only as *generic* library features (e.g. a provider adapter for `instrument()`, like the HuggingFace `InferenceClient` detection) — never as orchestration.

## Repo layout
```
cendor-libs/                  # repo root (cendorhq/cendor-libs)
├── CLAUDE.md  SKILLS.md  README.md  TESTING.md  PUBLISHING.md  CHANGELOG.md
├── CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md  INTEGRATION.md  LICENSE  NOTICE
├── pyproject.toml                 # uv workspace root
├── .github/workflows/             # ci.yml, release.yml
├── .claude/skills/                # project skills (new-package, namespace-guard, ...)
├── docs/                          # architecture.md + one page per package
└── packages/
    ├── cendor-core/          # src/cendor/core/   (NO src/cendor/__init__.py)
    ├── cendor-tokenguard/    # src/cendor/tokenguard/
    ├── cendor-libs/          # umbrella meta-package (pins all seven): pyproject only, no src/
    └── cendor/               # brand alias (depends on cendor-libs): pyproject only, no src/
```
`contextkit`, `squeeze`, `guardrails`, `cassette`, and `acttrace` each live under `packages/cendor-<tool>/` alongside these; add any future package with the `new-package` skill.

## Tech stack
Python ≥ 3.11 · **uv** (workspace, envs, build, publish) · **hatchling** (build backend) · **ruff** (lint+format) · **mypy** or **ty** (types) · **pytest** (+ pytest-asyncio) · OpenTelemetry GenAI semconv for spans.

## Conventions
- Full type hints on every public API; keep the public surface small; underscore-prefix internals.
- Google-style docstrings on public functions/classes.
- No heavy dependencies. Provider SDKs (openai, anthropic, ...) are **optional extras**, never hard deps.
- Support sync **and** async wherever a model/tool call is involved.
- Money is `Decimal`, never `float`.
- Every package has: `src/cendor/<tool>/`, `tests/`, `pyproject.toml`, `README.md`, and a `docs/<tool>.md`.

## Build order (current focus)
1. `cendor-core` (MVP slice: `types`, `tokens`, `prices`, `instrument`, `bus`, `otel`) + `cendor-tokenguard` → first release.
2. `contextkit` → 3. `squeeze` → 4. `cassette` → 5. `acttrace` → 6. `guardrails`. Grow `core` only as each needs it.

## Working in the monorepo
- `uv sync` — set up the workspace.
- `uv run pytest` — all tests; `uv run pytest packages/cendor-tokenguard` — one package.
- `uv run ruff check . && uv run ruff format .`
- Adding a package → use the **new-package** skill.
- Before any commit / build / release → run the **namespace-guard** skill.

## Definition of done (per package)
Typed public API · tests that hit no network · README with a one-line killer metric + a copy-paste example + a status badge · a `docs/<tool>.md` page · ruff clean · `import cendor.<tool>` works.

## Don't
- Don't add a top-level `src/cendor/__init__.py` (rule 1).
- Don't introduce tool→tool dependencies (rule 2).
- Don't add web servers, databases, or hosted services to a library.
- Don't expand `core`'s public API casually.
- Don't add agent-orchestration logic (loops, handoff, provider normalization) here — that's `cendor-sdk` (rule 7).
- Don't claim regulatory compliance anywhere.
- Don't add a `Co-Authored-By` trailer to git commits.

## Versioning — the org standard

The same rules apply to every Cendor repo, in both languages; this section is the copy that governs
**this** repo.

1. **A MAJOR bump needs the maintainer's explicit approval (Raghav). Never autonomous.** Propose it,
   say what breaks, wait. **Minor and patch need no approval** — ship them. Enforced by a
   maintainer-side pre-release gate (an internal cross-repo check, run outside this repo) that
   refuses any version crossing a major unless an `APPROVED-MAJOR` marker names the exact version.
2. **All libraries in one language share ONE major** — `@cendor/*` move together, `cendor-*` move
   together. Minors and patches stay independent per package.
3. **Majors are NOT coupled across languages.** The parity matrix is the contract, not matching
   numbers.
4. **Use minors.** A new capability is a **minor**; a fix is a **patch**. Do not drift into
   patch-patch-patch-then-a-surprise-major — the version number has to carry information.
