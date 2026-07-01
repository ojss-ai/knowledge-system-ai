---
description: Execute the next unchecked plan task (TDD, with review)
---

Execute exactly one task from the phase plans, following the kb-executing-plans skill:

1. Locate the next task: `docs/plans/README.md` → first incomplete phase → first unchecked `- [ ]` step's parent task. If $ARGUMENTS names a phase or task number, use that instead.
2. Verify you're on the phase branch (`phase-N-<name>`); create it from main if missing.
3. Read the skills listed in the plan's header before touching code.
4. Execute the task's steps in order. TDD steps are literal: write the failing test, RUN it, show the failure; implement minimally; RUN to green. Never reorder or merge steps.
5. If the plan conflicts with the actual codebase, follow the "When the plan is wrong" protocol in kb-executing-plans — do not improvise silently.
6. Tick the task's checkboxes in the plan file and commit (conventional commit, plan edit included).
7. Run the two-stage review yourself if subagents are unavailable: first spec compliance (diff vs task text), then code quality (red-flag lists in the skills). Fix CRITICAL findings now.
8. Finish by reporting: what was done, test evidence (paste output), and what the next task is.

One task only. Do not continue to the next task without being asked.
