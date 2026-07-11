# MCP — live docs for your assistant

The [rules files](assistant-rules.md) you paste are a static snapshot. If your assistant runs in
**agent mode** — Claude Code, Cursor's agent, GitHub Copilot agent, Windsurf Cascade — there's a live
option: the **Cendor MCP server**. Connect it once and your assistant can *look up* the correct
call-shape on demand — the same [trap table and canonical examples](for-ai-assistants.md), served
fresh — instead of relying on a pasted snapshot that drifts as Cendor ships.

It is **read-only** and **pull-based**: your assistant calls a tool, the server answers, your
assistant writes the code. Your codebase never flows to the server — only the query arguments your
assistant sends.

## Two ways to connect

- **Remote** (zero-install, always current): `https://mcp.cendor.ai`
- **Local** (fully offline, docs bundled — nothing leaves your machine): `npx @cendor/mcp` (Node) or
  `uvx cendor-mcp` (Python)

The fastest path is `npx @cendor/init --mcp` (or `uvx cendor-init --mcp`), which writes the connect
config below into your repo (`.cursor/mcp.json` / `.vscode/mcp.json`) so you don't hand-edit it — see
[init CLI & doctor](assistant-init.md).

### Claude Code

```bash
# remote (recommended — always current)
claude mcp add --transport http cendor https://mcp.cendor.ai
# local, offline
claude mcp add cendor -- npx -y @cendor/mcp
```

### Cursor — `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "cendor": { "url": "https://mcp.cendor.ai" }
  }
}
```

### GitHub Copilot (agent mode) — `.vscode/mcp.json`

```json
{
  "servers": {
    "cendor": { "type": "http", "url": "https://mcp.cendor.ai" }
  }
}
```

### Windsurf — `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "cendor": { "serverUrl": "https://mcp.cendor.ai" }
  }
}
```

### Local / offline — any MCP client

Swap the remote URL for a stdio command. The docs are bundled, so it runs with no network:

```json
{
  "mcpServers": {
    "cendor": { "command": "npx", "args": ["-y", "@cendor/mcp"] }
  }
}
```

```json
{
  "mcpServers": {
    "cendor": { "command": "uvx", "args": ["cendor-mcp"] }
  }
}
```

## The five tools

Every answer is built from the docs [source of truth](https://cendor.ai/docs) and stamped with the current published
package versions — so the server never teaches a shape newer than what's on
[PyPI / npm](https://cendor.ai/releases).

| Tool | What it returns |
|---|---|
| `search_docs(query)` | Full-text search over the docs → matching sections with their `cendor.ai` URLs. |
| `get_page(slug)` | A full docs page as markdown — `"tokenguard"`, `"getting-started"`, `"sdk/agents"`. |
| `get_api(symbol, lang?)` | The anti-hallucination call-shape lookup: the current correct shape + the common wrong one. `lang` is `"python"` or `"ts"` (aliases `py` / `js` accepted); omit it for both. |
| `example(task, lang?)` | A runnable, CI-typechecked snippet for a task (`"budget a loop"`, `"gate input"`). Same `lang` values as `get_api`. |
| `list_recipes()` | The [cookbook](https://cendor.ai/cookbook) index — copy-paste recipes that run offline, grouped by category. |

Full setup for every assistant, with copy buttons: [cendor.ai/mcp](https://cendor.ai/mcp).

## How it stays honest

- **One source of truth.** The server's content is built from the same library-repo docs this site is
  built from. Fix a doc, rebuild — the site and the MCP answers move together. No forked copy to drift.
- **Read-only, pull-based.** Your assistant calls a tool; the server responds. It never pushes into
  your editor and makes no server→client calls (no sampling / elicitation). Only the query arguments
  your assistant sends reach the server.
- **The libraries need no server.** Cendor is local-first. This MCP server is optional developer
  tooling for wiring an assistant up — no Cendor library requires it, or any server, at runtime.

## Honest limits

- MCP is only called by **agent** modes. Inline autocomplete does **not** call it — for that path,
  rely on the types Cendor ships in every package (the inline `@example` + correct-shape signatures).
  Use both: MCP for agents, the shipped types everywhere.
- The **remote** endpoint receives the tool arguments your assistant sends (a symbol name, a search
  query) — that's the trade for zero-install and always-current answers. The **local** server
  (`npx @cendor/mcp` / `uvx cendor-mcp`) bundles the docs and runs fully offline if you'd rather send
  nothing at all.
- The server states call **shapes**, never performance numbers, and stamps answers with the published
  version — so it can't teach a shape newer than what you can install. Open source (Apache-2.0):
  [github.com/cendorhq/cendor-mcp](https://github.com/cendorhq/cendor-mcp).
