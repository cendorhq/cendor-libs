# init CLI & doctor

`cendor-init` is **one command to make a project Cendor-ready and Cendor-fluent for its AI
assistant** — it writes the [rules files](assistant-rules.md), can add the [MCP](assistant-mcp.md)
connect config, and can scaffold a correct `instrument()` call. A companion `doctor` static-checks
your wiring and exits non-zero on hard problems, so it fits CI. Offline: no network, no API key, and
no Cendor library depends on it at runtime — it's optional developer tooling.

```bash
npx @cendor/init            # Node — detect + write assistant rules (idempotent)
uvx cendor-init             # Python — same behavior, stdlib-only
npx @cendor/init doctor     # validate wiring; exit 1 on hard problems (CI-usable)
```

Both entry points share the same behavior; use whichever matches your project.

## What `init` does

1. **Detects your project** — Node (`package.json`) or Python (`pyproject.toml` / `requirements`),
   which provider SDKs you have, and which `@cendor/*` / `cendor-*` packages are installed.
2. **Writes the matching assistant rules file(s)** so your assistant reads the correct call-shapes on
   every edit — no need to paste anything. Detected by default; `--all` for every one. The five
   targets (four distinct blocks — Windsurf reuses the `AGENTS.md` body):

   | Assistant | File |
   |---|---|
   | GitHub Copilot | `.github/copilot-instructions.md` |
   | Cursor | `.cursor/rules/cendor.mdc` |
   | Cross-tool (always written) | `AGENTS.md` |
   | Claude Code | a marked section in `CLAUDE.md` |
   | Windsurf | `.windsurf/rules` |

   **Idempotent and safe:** re-running updates a marker-delimited block in place — never duplicates,
   never clobbers your surrounding content. A dedicated file it didn't create is left alone unless you
   pass `--force`.
3. **Offers MCP setup** (`--mcp`) — drops the [MCP](assistant-mcp.md) connect config (`.cursor/mcp.json`
   / `.vscode/mcp.json`) where it's absent.
4. **Optional starter** (`--scaffold`) — a minimal, correct `instrument()` + budgeted-call example in
   your language.

The rules content is a copy of the [rules files](assistant-rules.md) — the single place these traps
live, so `init` never forks the wording.

## Options

| Flag | Effect |
|---|---|
| `--all` | write every assistant rules file, not just the detected ones |
| `--assistant <list>` | comma-separated subset: `copilot,cursor,agents,claude,windsurf` |
| `--mcp` | also drop MCP connect config (`.cursor/mcp.json`, `.vscode/mcp.json`) where absent |
| `--scaffold` | also write a correct `instrument()` + budget starter for this project |
| `--force` | overwrite an owned file (`.cursor/rules/cendor.mdc`) even if it isn't ours |
| `--dry-run` | show what would change without writing anything |

## What `doctor` checks

Static checks only — it **never mutates** your project, and exits non-zero on hard problems so it
works in CI:

| Check | Flags |
|---|---|
| **Namespace** | a stray `cendor/__init__.py` in your tree, or a bare `import cendor` (the namespace has no module body — import `from cendor.<tool>`) |
| **Provider deps** | a provider SDK your code imports but hasn't installed/declared (Cendor never pulls one for you — they're optional extras) |
| **`instrument()` once** | Cendor is imported but the client is never wrapped (nothing is observed) |
| **Money** | a price/cost coerced to `float` / `number` (it should stay `Decimal` / `decimal.js`) |
| **Versions** | an installed/pinned `cendor-*` / `@cendor/*` version trails the latest release |
| **Lockfiles** | a `uv.lock` / `package-lock.json` / `pnpm-lock.yaml` pinning Cendor below the latest — **the range is not always the constraint**, and a lock keeps a build green while it silently stays behind |

### In CI

`doctor` returns a non-zero exit code when it finds a hard problem, so a one-line job keeps wiring
mistakes out of `main`:

```bash
npx @cendor/init doctor     # Node
uvx cendor-init doctor      # Python
```

## Keeping Cendor up to date

**Cendor never upgrades itself, and never checks for updates at runtime.** No library opens a socket
you did not ask for. Your package manager owns your versions — a governance library that changed what
it blocks under a running system would be the opposite of useful, and an audit trail you cannot tie to
a known version is worth less.

So the tooling checks only when *you* ask:

```bash
uvx cendor-init doctor            # offline: compares against a snapshot bundled in the CLI
uvx cendor-init doctor --online   # live: reads https://cendor.ai/releases.json
```

`--online` is opt-in for a reason: without it there is **no network call at all**. Use it in CI, where
the CLI itself is usually pinned and the bundled snapshot can be arbitrarily old. If the feed is
unreachable, `doctor` says so and falls back to the snapshot rather than failing — being offline is
not a wiring problem.

### Let your bot do it

The honest answer for a team is the one you already use. `cendor-*` and `@cendor/*` are ordinary
packages; point Renovate or Dependabot at them and group them so the whole family moves together —
which matters more than usual on npm, because two copies of `@cendor/core` means two event buses and
the libraries stop cooperating with no error at all.

```json
// renovate.json — group the whole Cendor family into ONE PR
{
  "extends": ["config:recommended"],
  "packageRules": [
    {
      "groupName": "cendor",
      "matchPackageNames": ["/^@cendor//", "/^cendor(-|$)/"],
      "semanticCommitType": "chore"
    }
  ]
}
```

```yaml
# .github/dependabot.yml — same idea, both ecosystems
version: 2
updates:
  - package-ecosystem: npm
    directory: /
    schedule: { interval: weekly }
    groups:
      cendor: { patterns: ["@cendor/*"] }
  - package-ecosystem: pip
    directory: /
    schedule: { interval: weekly }
    groups:
      cendor: { patterns: ["cendor-*", "cendor"] }
```

Machine-readable current versions live at
**[`cendor.ai/releases.json`](https://cendor.ai/releases.json)** (the human page is
[/releases](https://cendor.ai/releases)). Fields are only ever added, never renamed or removed.

## Honest limits

- `init` writes **rules files and config**, and can scaffold a starter — it does **not** install
  Cendor or a provider SDK for you (those stay your explicit choice). It makes no network call.
- `doctor`'s version check is an **offline hint** from a bundled snapshot unless you pass `--online`;
  the snapshot can lag a very recent release. The live source of truth is
  [/releases](https://cendor.ai/releases) / [/releases.json](https://cendor.ai/releases.json).
- The lockfile check reads the lock as **text** — it reports what is pinned, it does not resolve. It
  will not tell you *why* a resolver chose a version, only that the pin is behind.
- The rules `init` writes are a static snapshot — for a live lookup use the [MCP
  server](assistant-mcp.md) (agent mode) or the types shipped in every package. All three stack.
