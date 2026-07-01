# ADR-009: tree-sitter for codebase parsing

**Status:** Accepted · 2026-06-12

## Context
The codebase scanner must extract modules, symbols, imports, and best-effort call edges across languages. Candidates: language-native AST libs (ast, ts-compiler), LSP servers, tree-sitter.

## Decision
tree-sitter with per-language grammar bindings (`tree_sitter_python`, `tree_sitter_typescript` for v1). One `LanguageParser` protocol in `tools/kb_codebase_scan/parsers/`; each language implements `extract(file) -> Symbols, Imports, Calls`. Call edges carry a `confidence` property — static call resolution is best-effort by design.

## Consequences
- Uniform, fast, error-tolerant parsing; adding a language = one parser class + grammar dep.
- No type-aware resolution (that would need LSP); cross-file call edges are heuristic. Acceptable: graph is for navigation, not refactoring.
- LLM summaries (ADR-010) complement structure with meaning.

## Revisit when
Users need precise call graphs — evaluate SCIP/LSIF indexers then.
