---
name: kb-executing-plans
description: Use when executing docs/plans/* — task selection, subagent dispatch, review gates, and re-planning
---

# Executing Plans

## Overview
Plans in `docs/plans/` are written for an enthusiastic junior with no context: exact paths, complete code, verification steps. Your job is faithful execution with review gates — not creative reinterpretation.

## The loop
1. `docs/plans/README.md` → first incomplete phase → open its plan → first unchecked `- [ ]` task.
2. Read the skills listed in the plan header. Confirm the branch is `phase-N-<name>`.
3. Execute the task's steps IN ORDER. TDD steps are literal: run the test, paste the failure, then implement.
4. Tick the checkbox in the same commit as the work.
5. Two-stage review (below). Fix criticals before the next task.
6. Phase end: run `/kb-verify`, then the phase's exit criteria from the plan header.

## Subagent dispatch (preferred for fresh context)
- Dispatch `kb-implementer` with: the full task text, the plan header, paths to required skills. One task per agent — never batch.
- Then `kb-spec-reviewer` (did the diff implement exactly the task? nothing more, nothing less?) and `kb-code-reviewer` (quality, conventions, red-flag scan from the skills).
- Reviewers report findings as `CRITICAL` / `IMPORTANT` / `NIT`. CRITICAL blocks; IMPORTANT fixes within the phase; NIT optional.
- Orchestrator never implements while a worker is dispatched on the same files.

## When the plan is wrong
Plans drift from reality (an earlier task changed an API, a library version differs). Protocol:
1. STOP the task. Do not improvise around it silently.
2. Small mismatch (renamed symbol, moved file): fix the plan text in the same commit, note `[plan-fix]` in the commit body, continue.
3. Structural mismatch (approach can't work): dispatch `kb-architect` to rewrite the remaining tasks of the phase against current reality; human approves the rewritten plan before execution resumes.
4. Never mark a task complete "with deviations" — the plan must end up describing what the code actually is.

## Task hygiene
- A task is complete only with: green tests (output as evidence), checkbox ticked, conventional commit(s), reviews passed.
- Blocked > 30 min on environment issues: record the blocker in the plan file under `## Blockers`, move to the next independent task if one exists, surface to human otherwise.
- Never skip ahead past unchecked tasks unless they're explicitly marked independent.

## Phase exit checklist
- [ ] All tasks checked, all CRITICAL/IMPORTANT findings resolved
- [ ] `/kb-verify` green (full suite, lint, types, visibility audit)
- [ ] Exit criteria in plan header demonstrated with evidence
- [ ] `docs/plans/README.md` status table updated
- [ ] Phase branch merged via PR (human approves)
