---
description: Report progress across all phase plans
---

Report implementation progress for the Knowledge Base project:

1. Read `docs/plans/README.md` and every `docs/plans/phase-*.md`.
2. For each phase, count checked vs unchecked `- [ ]` task steps, and read its status row.
3. Check `git log --oneline -10` and current branch for context.
4. Output a compact table: Phase | Tasks done/total | Status | Blockers (from any `## Blockers` sections).
5. Identify THE next actionable task (first unchecked step of first incomplete phase) and print its task heading and file.
6. If `docs/plans/README.md`'s status table disagrees with actual checkboxes, fix the table.

Do not start implementing anything — this command only reports.
