# claude-kit — Agentic Development Kit

Superpowers/ruflo-style kit for building the Knowledge Base System with Claude Code.

## Install (one time)

```bash
bash claude-kit/install.sh          # macOS/Linux
powershell -ExecutionPolicy Bypass -File claude-kit\install.ps1   # Windows
```

Copies into `.claude/` (skills, commands, agents auto-discovered by Claude Code). `claude-kit/` stays the version-controlled source of truth; re-run after edits.

## What's inside

| Layer | Contents |
|---|---|
| `CLAUDE.md` (repo root) | Operating manual: workflow loop, invariants, skill index |
| `skills/` (10) | kb-conventions · kb-tdd-workflow · kb-visibility-filter · kb-neo4j-graph · kb-pgvector-search · kb-api-conventions · kb-celery-jobs · kb-frontend-graph · kb-ingestion-connectors · kb-executing-plans |
| `commands/` (5) | `/kb-status` · `/kb-next-task` · `/kb-review` · `/kb-verify` · `/kb-new-adr` |
| `agents/` (4) | kb-implementer · kb-spec-reviewer · kb-code-reviewer · kb-architect |
| `docs/decisions/` | 11 ADRs (the "why" behind every architectural choice) |
| `docs/plans/` | Atomic phase plans — bite-sized TDD tasks with complete code |

## The loop

```
/kb-status  →  /kb-next-task (or dispatch kb-implementer)  →  /kb-review  →  repeat
                                  phase boundary → /kb-verify → PR
```

Plans drift? The kb-executing-plans skill defines the re-planning protocol (kb-architect rewrites, human approves). Decisions change? `/kb-new-adr` — never silently.
