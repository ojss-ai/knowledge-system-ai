# tools/kb-codebase-scan/language_parser.py
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class SymbolKind(enum.StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


@dataclass
class ParsedSymbol:
    name: str
    kind: SymbolKind
    fqn: str  # fully-qualified name: module.Class.method
    line_start: int
    line_end: int
    docstring: str = ""
    parent_fqn: str | None = None  # for methods: parent class FQN
    calls: list[str] = field(default_factory=list)  # names of called symbols
    confidence: float = 1.0  # 0.0-1.0; call edges are 0.7 (heuristic)


@dataclass
class ParsedFile:
    file_path: str  # relative to repo root
    language: str
    module_fqn: str  # e.g. "app.services.node_service"
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    error: str | None = None  # parse error if any


@runtime_checkable
class LanguageParser(Protocol):
    @property
    def extensions(self) -> list[str]:
        """File extensions handled by this parser (e.g. ['.py'])."""
        ...

    def parse(self, file_path: str, source: str) -> ParsedFile:
        """Parse source text and return structured symbols."""
        ...
