# Publishing cendor-libs

The end-to-end check-in + release runbook is the **`release-package` skill**, not this file — this is
only a pointer so the flow is discoverable from the repo root.

- **Runbook (canonical):** [`.claude/skills/release-package/SKILL.md`](.claude/skills/release-package/SKILL.md)
  — pre-flight green gate → per-package version + CHANGELOG bump → check-in to `main` → push tags →
  watch runs → verify on PyPI. It is `disable-model-invocation: true` (manual only; invoke with
  `/release-package`).

## The one thing to remember

- **A push to `main` publishes nothing.** Releases trigger **only on tags** matching `*-v*.*.*`.
- **Tag pattern:** `<tool>-vX.Y.Z` — e.g. `core-v1.11.1`, `tokenguard-v1.5.1`, `squeeze-v1.1.1`,
  `libs-v1.2.0` (the umbrella), `cendor-v1.1.0` (the brand alias). The tool segment selects the
  package **and** the per-project PyPI trusted-publisher environment; `release.yml` hard-guards
  `tag version == pyproject version` before building.
- Bump only the packages whose content changed; never re-tag an unchanged version (PyPI rejects a
  duplicate upload and the run fails). Push **≤ 3 tags per `git push`**.

Versions are **independent across languages** — parity is documented (see
[`docs/languages.md`](docs/languages.md)), never version-coupled. The TypeScript port publishes from
`cendor-libs-js` via changesets (its own `PUBLISHING.md`).
