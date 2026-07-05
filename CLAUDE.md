# CLAUDE.md — cendor

Project **constitution**. Always in effect. Locked decisions → `MEMORY.md` (a local-only, gitignored working file — not committed). Task playbooks → `SKILLS.md` and `.claude/skills/`. Deep design → `docs/`.

## What this is
Cendor — *"Production plumbing for LLM applications."* A monorepo publishing a family of small, composable Python libraries that sit **beneath** agent frameworks: context, cost, testing, governance. Framework-agnostic. Local-first. Apache-2.0.

Pipeline: `contextkit` (assemble) → `squeeze` (compress) → `tokenguard` (budget) → `cassette` (test) → `acttrace` (audit), on a shared foundation `cendor-core`. All under the `cendor.*` import namespace.

## Cardinal rules — DO NOT BREAK
1. **NEVER create `src/cendor/__init__.py`.** `cendor` is a PEP 420 implicit namespace package. A top-level `__init__.py` breaks every cross-package import. Each package owns only `src/cendor/<tool>/` (which *does* have its own `__init__.py`).
2. **Tools never import each other.** They cooperate only through `cendor-core` (shared types/protocols + event bus) or an optional extra (e.g. `contextkit[squeeze]`). No tool→tool hard dependency.
3. **Keep `cendor-core` tiny and stable.** It is the blast radius for the whole stack. Add to it only what a tool needs, when it needs it. Never big-design core up front.
4. **Local-first, no servers.** No library may require an account, network, or running infrastructure. Cloud and OpenTelemetry export are always optional.
5. **Don't ship empty packages.** A package reaches PyPI only with tests + a README + a real `v0`.
6. **`acttrace` produces *evidence*, not a guarantee.** Never claim "EU AI Act compliant" — it provides evidence to *support* compliance.

## Repo layout
```
cendor/                       # repo root (cendorhq/Cendor)
├── CLAUDE.md  SKILLS.md  README.md  LICENSE   # MEMORY.md is a local-only, gitignored working file
├── pyproject.toml                 # uv workspace root
├── .github/workflows/             # ci.yml, release.yml
├── .claude/skills/                # project skills (new-package, namespace-guard, ...)
├── docs/                          # architecture.md + one page per package
└── packages/
    ├── cendor-core/          # src/cendor/core/   (NO src/cendor/__init__.py)
    ├── cendor-tokenguard/    # src/cendor/tokenguard/
    └── cendor/               # umbrella meta-package: pyproject only, no src/
```
`contextkit`, `squeeze`, `cassette`, and `acttrace` each live under `packages/cendor-<tool>/` alongside these; add any future package with the `new-package` skill.

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
2. `contextkit` → 3. `squeeze` → 4. `cassette` → 5. `acttrace`. Grow `core` only as each needs it.

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
- Don't claim regulatory compliance anywhere.
- Don't add a `Co-Authored-By` trailer to git commits.
