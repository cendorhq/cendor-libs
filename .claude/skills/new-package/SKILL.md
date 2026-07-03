---
name: new-package
description: Scaffold a new cendor-<tool> package in the uv workspace with the correct PEP 420 namespace layout. Use when adding a new library, tool, or package to the cendor monorepo.
---
# Scaffold a new cendor package

Given a tool name `<tool>` (lowercase, hyphen-free, e.g. `contextkit`):

1. Create `packages/cendor-<tool>/` containing:
   - `pyproject.toml` (template below)
   - `src/cendor/<tool>/__init__.py`   ← the subpackage `__init__.py` IS required
   - `tests/test_<tool>.py`
   - `README.md` (use the **package-readme** skill)
2. **Do NOT create `packages/cendor-<tool>/src/cendor/__init__.py`.** The namespace root must never have an `__init__.py` (see CLAUDE.md rule 1).
3. Ensure the package is picked up by the workspace (`members = ["packages/*"]` in the root `pyproject.toml`).
4. If it uses the foundation, depend on `cendor-core>=1.0,<2.0`. If it composes with another tool, do it via a `core` protocol + an optional extra — never a direct tool→tool dependency.
5. `uv sync`, then `uv run pytest packages/cendor-<tool>`.
6. Run the **namespace-guard** skill to confirm the layout invariant holds.

## pyproject.toml template
```toml
[project]
name = "cendor-<tool>"
version = "0.0.0"
description = "<one-line role>"
requires-python = ">=3.11"
license = "Apache-2.0"
dependencies = ["cendor-core>=1.0,<2.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/cendor"]   # contributes cendor/<tool> only
```

## Verify
`import cendor.<tool>` works, and `find packages -path '*/src/cendor/__init__.py'` prints nothing.
