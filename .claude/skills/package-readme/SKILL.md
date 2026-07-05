---
name: package-readme
description: Write or update a cendor package README in the house style — a one-line killer metric, a runnable copy-paste example, and a status badge. Use when creating or updating any package README.
---
# Package README house style

Keep it tight. Structure:

1. `# cendor-<tool>` + a one-sentence pitch of its role.
2. **Killer metric** on its own line (e.g. *"80% smaller, 100% reversible"* / *"Caught a $40 runaway loop before the first dollar"*).
3. Status: 🚧 building / ✅ stable.
4. `pip install cendor-<tool>`
5. A 6–12 line copy-paste example that actually runs.
6. A short "How it plugs into your agent" note if relevant (inbound vs wrap-around — see docs/architecture.md §4).
7. Footer: *"Part of the Cendor stack — github.com/cendorhq/cendor-libs. Powered by PowerAI Labs."*.

Lead with the metric. Show, don't tell. No marketing fluff, no feature dump.
