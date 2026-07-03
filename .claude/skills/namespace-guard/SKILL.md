---
name: namespace-guard
description: Verify the cendor PEP 420 namespace packaging is correct — there must be no src/cendor/__init__.py at the namespace root. Use before committing, building, or releasing, or whenever cross-package imports break.
---
# Namespace guard

The single most important invariant in this repo.

```bash
# Must print NOTHING:
find packages -path '*/src/cendor/__init__.py' -print
```

- If it prints any path → **delete that file.** A top-level `cendor/__init__.py` turns the implicit namespace into a regular package and silently breaks every other `cendor.<tool>` import.
- Each `src/cendor/<tool>/__init__.py` SHOULD exist — only the `src/cendor/__init__.py` *level* is forbidden.
- Confirm the umbrella `packages/cendor/` ships **no** `src/` directory and only declares dependencies in its `pyproject.toml`.

If all checks pass, report "namespace-guard: OK".
