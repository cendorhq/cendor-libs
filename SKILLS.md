# SKILLS.md — project skill plan

Reusable playbooks for recurring work in this repo. Full skill files live in `.claude/skills/<name>/SKILL.md` and Claude Code discovers them automatically (some are manual-only). Think of `CLAUDE.md` as the constitution (always on) and these skills as the laws (invoked when applicable).

| Skill | When it triggers | What it does | Invocation |
|---|---|---|---|
| **new-package** | adding a new `cendor-<tool>` | scaffolds a package with the correct namespace layout, `pyproject.toml`, tests, README | auto / `/new-package` |
| **namespace-guard** | before commit/build/release, or when cross-package imports break | verifies there is no top-level `src/cendor/__init__.py`; checks the layout invariant | auto / `/namespace-guard` |
| **write-tests** | writing or reviewing tests | enforces testing conventions (pytest, mocked clients, golden token counts, no network) | auto (reference) |
| **add-provider-adapter** | adding an LLM provider (gemini, bedrock, ollama, ...) | wires the provider into core's `tokens`, `prices`, and `instrument` as an optional extra | auto / `/add-provider-adapter` |
| **package-readme** | creating/updating a package README | writes the house-style README (killer metric, example, badge) | auto / `/package-readme` |
| **release-package** | checking work into `main` and publishing to PyPI | full flow: green-gate → bump/changelog → **merge to main** → push **tags** (≤3/push, core first) → verify on PyPI → record. Covers the merge step and tag-only-triggers-release gotcha that trip sessions up | **manual only** (`/release-package`) |

Add new skills as recurring tasks emerge — e.g. a `benchmark` skill to produce the token-reduction / cost-savings numbers.
