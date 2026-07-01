---
description: Record a new architecture decision (ADR)
---

Create a new ADR in `docs/decisions/` for the decision described in $ARGUMENTS (ask for the decision if no arguments given).

1. Read `docs/decisions/README.md` to find the next number and check no existing ADR already covers this (if one does and the decision changes it, this ADR **supersedes** it — say so in both files).
2. Interview the human briefly if the context is unclear: what problem forces a choice? what alternatives were considered? what tips the balance?
3. Write `docs/decisions/ADR-NNN-<kebab-slug>.md` using the house format:
   - `# ADR-NNN: <decision as a statement>`
   - `**Status:** Proposed · <today's date>` (human flips to Accepted)
   - `## Context` — the forces, 2-4 sentences, no fluff
   - `## Decision` — what we will do, concrete (names, modules, configs)
   - `## Consequences` — honest costs and benefits, including what becomes harder
   - `## Revisit when` — observable trigger conditions, not "later"
4. Add the row to the index table in `docs/decisions/README.md`.
5. If the decision changes the canonical vocabulary or an invariant, flag which skills/CLAUDE.md sections need a matching edit — and make those edits.

Keep it under 40 lines. ADRs are read by agents before every related task; verbosity is a tax.
