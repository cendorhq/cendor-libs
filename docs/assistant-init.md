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
| **Versions** | an installed/pinned `cendor-*` / `@cendor/*` version trails the latest release (an offline hint from a bundled snapshot — the live truth is [/releases](/releases)) |

### In CI

`doctor` returns a non-zero exit code when it finds a hard problem, so a one-line job keeps wiring
mistakes out of `main`:

```bash
npx @cendor/init doctor     # Node
uvx cendor-init doctor      # Python
```

## Honest limits

- `init` writes **rules files and config**, and can scaffold a starter — it does **not** install
  Cendor or a provider SDK for you (those stay your explicit choice). It makes no network call.
- `doctor`'s version check is an **offline hint** from a bundled snapshot; it can lag a very recent
  release. The live source of truth is [/releases](/releases).
- The rules `init` writes are a static snapshot — for a live lookup use the [MCP
  server](assistant-mcp.md) (agent mode) or the types shipped in every package. All three stack.
