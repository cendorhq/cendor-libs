---
name: release-package
description: Check in work to main and release cendor packages to PyPI via git tags + OIDC trusted publishing. Manual only — invoke deliberately with /release-package, never automatically.
disable-model-invocation: true
---
# Check-in & release (MANUAL ONLY)

Never run as part of normal work. This is the end-to-end flow for landing a change on `main` and
publishing the affected packages. **Two separate things:** "check in" = merge to `main`; "release" =
push version **tags** (a branch/main push alone publishes nothing — `release.yml` triggers only on
tags `*-v*.*.*`).

## 0. Pre-flight green gate (on the working branch)
All must pass before merging or tagging:
```bash
find packages -path '*/src/cendor/__init__.py' -print          # namespace-guard: must print NOTHING
uv run ruff check . && uv run ruff format --check .
uv run mypy -p cendor.core -p cendor.contextkit -p cendor.squeeze \
            -p cendor.tokenguard -p cendor.cassette -p cendor.acttrace
uv run pytest -q
```

## 1. Version + changelog (per changed package only)
- Bump `version` in each changed `packages/cendor-<tool>/pyproject.toml`.
- Add a `## [X.Y.Z] — <date>` section to that package's `CHANGELOG.md` **and** a `[X.Y.Z]: https://pypi.org/project/...` link-reference (release notes are auto-extracted from this section).
- Leave a package's version **unchanged** if its content didn't change. The **umbrella** `cendor` has unpinned deps, so usually leave it as-is — do **not** re-tag an unchanged version (PyPI rejects duplicate uploads → the release run fails).
- `uv lock` so `uv.lock` matches the bumped versions; commit it.
- **Prices snapshot freshness (P14 — check when releasing `core`):** the bundled offline price table
  carries an `_updated` stamp. If it is stale (models added / rates moved since that date), regenerate
  the snapshot from the officially-verified source and commit it with the `core` bump, so the offline
  default stays honest (`truth = the product`). **Never hand-edit a rate** — only the generator's output.

## 2. Check in to `main`
Commits: author is the repo owner, **no `Co-Authored-By: Claude` trailer**.
```bash
git checkout main
git merge --ff-only <feature-branch>     # clean fast-forward when the branch is ahead / 0-behind
git push origin main
```
If the branch is behind `main`, `git rebase main` on the branch first, then fast-forward.

⚠️ **Permission caveat (this is what blocks some sessions):** certain permission modes refuse
`git push` to `main` via an auto-classifier — do **not** circumvent it. If blocked, either:
- open a PR from the branch (`https://github.com/<org>/<repo>/pull/new/<branch>`) and merge it on GitHub, or
- have the user authorize `git push` for the session.

## 3. Release = push tags (in batches of ≤3)
Tag each **changed** package at the merged commit. **`core` first** (other tools may pin a new core).
**Push ≤3 tags per `git push`** — pushing >3 tags at once triggers **zero** workflow runs (silent).
```bash
git tag core-vX.Y.Z contextkit-vX.Y.Z squeeze-vX.Y.Z
git push origin core-vX.Y.Z contextkit-vX.Y.Z squeeze-vX.Y.Z      # batch 1
git push origin tokenguard-vX.Y.Z cassette-vX.Y.Z acttrace-vX.Y.Z # batch 2
```
`release.yml` builds `uv build --package cendor-<tool>`, publishes via the per-project PyPI
**trusted publisher** (OIDC, no tokens; GitHub environment name == the tag's tool segment), and cuts
a GitHub Release with notes from the CHANGELOG.

## 4. Watch the runs (gh is usually NOT installed)
Use the GitHub REST API with the cached git credential — never print the token:
```bash
token=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | sed -n 's/^password=//p')
curl -s -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/cendorhq/cendor-libs/actions/runs?per_page=20"
```

## 5. Verify on PyPI
The `/pypi/<name>/json` "latest" view is CDN-cached and **lags** minutes — check the **version-specific** endpoint:
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/cendor-core/X.Y.Z/json   # 200 = landed
```

## 6. Record
Update the **Live versions** line in `MEMORY.md`.

## First-time name reservation
Publish a `0.0.0` stub the same way; the package's PyPI trusted publisher must already exist (env name
== tool segment), and PyPI allows ≤3 *pending* publishers at once. See personal memory
`pypi-release-gotchas` for the full set (contents:read in CI, per-project env, ≤3 tags/push).
