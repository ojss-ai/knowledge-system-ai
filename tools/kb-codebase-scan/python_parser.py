# tools/kb-codebase-scan/python_parser.py
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from language_parser import ParsedFile, ParsedSymbol, SymbolKind

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Node, Parser

    _PY_LANG = Language(tspython.language())
    _HAS_TREE_SITTER = True
except ImportError:
    _HAS_TREE_SITTER = False


def _fqn_from_path(file_path: str) -> str:
    """Convert file path to Python module FQN. 'app/services/node.py' -> 'app.services.node'."""
    p = Path(file_path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class PythonParser:
    """
    Python source parser using tree-sitter.
    Falls back to regex-based AST extraction if tree-sitter is unavailable.
    """

    @property
    def extensions(self) -> list[str]:
        return [".py"]

    def parse(self, file_path: str, source: str) -> ParsedFile:
        module_fqn = _fqn_from_path(file_path)
        pf = ParsedFile(file_path=file_path, language="python", module_fqn=module_fqn)

        if _HAS_TREE_SITTER:
            return self._parse_tree_sitter(pf, source)
        return self._parse_regex_fallback(pf, source)

    def _parse_tree_sitter(self, pf: ParsedFile, source: str) -> ParsedFile:
        parser = Parser(_PY_LANG)
        tree = parser.parse(source.encode())
        root = tree.root_node

        # Collect imports
        for node in root.children:
            if node.type in ("import_statement", "import_from_statement"):
                pf.imports.append(source[node.start_byte : node.end_byte].strip())

        # Walk function/class definitions
        self._walk_node(root, pf, source, parent_fqn=None, class_fqn=None)
        return pf

    def _walk_node(
        self,
        node: Node,
        pf: ParsedFile,
        source: str,
        parent_fqn: str | None,
        class_fqn: str | None,
    ) -> None:
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            name = source[name_node.start_byte : name_node.end_byte] if name_node else "Unknown"
            fqn = f"{pf.module_fqn}.{name}"
            doc = self._extract_docstring(node, source)
            sym = ParsedSymbol(
                name=name,
                kind=SymbolKind.CLASS,
                fqn=fqn,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                docstring=doc,
            )
            pf.symbols.append(sym)
            # Walk class body for methods
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    self._walk_node(child, pf, source, parent_fqn=fqn, class_fqn=fqn)

        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            name = source[name_node.start_byte : name_node.end_byte] if name_node else "unknown"
            kind = SymbolKind.METHOD if class_fqn else SymbolKind.FUNCTION
            fqn = f"{parent_fqn or pf.module_fqn}.{name}"
            doc = self._extract_docstring(node, source)
            calls = self._extract_calls(node, source)
            sym = ParsedSymbol(
                name=name,
                kind=kind,
                fqn=fqn,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                docstring=doc,
                parent_fqn=parent_fqn,
                calls=calls,
                confidence=1.0,
            )
            pf.symbols.append(sym)
        else:
            for child in node.children:
                self._walk_node(child, pf, source, parent_fqn=parent_fqn, class_fqn=class_fqn)

    def _extract_docstring(self, node: Node, source: str) -> str:
        body = node.child_by_field_name("body")
        if not body:
            return ""
        first = next((c for c in body.children if c.type not in ("comment",)), None)
        if first and first.type == "expression_statement":
            expr = first.children[0] if first.children else None
            if expr and expr.type in ("string", "concatenated_string"):
                raw = source[expr.start_byte : expr.end_byte]
                return raw.strip("\"'").strip()
        return ""

    def _extract_calls(self, func_node: Node, source: str) -> list[str]:
        calls: list[str] = []
        for node in self._iter_nodes(func_node):
            if node.type == "call":
                func_part = node.child_by_field_name("function")
                if func_part:
                    call_text = source[func_part.start_byte : func_part.end_byte]
                    # Get last segment (foo.bar -> bar)
                    name = call_text.split(".")[-1]
                    calls.append(name)
        return list(set(calls))

    def _iter_nodes(self, node: Node) -> Iterator[Node]:
        yield node
        for child in node.children:
            yield from self._iter_nodes(child)

    def _parse_regex_fallback(self, pf: ParsedFile, source: str) -> ParsedFile:
        """Minimal regex-based parser for environments without tree-sitter."""
        import_re = re.compile(r"^(?:import|from)\s+(\S+)", re.MULTILINE)
        class_re = re.compile(r"^class\s+(\w+)", re.MULTILINE)
        func_re = re.compile(r"^(?:    )?def\s+(\w+)\s*\(", re.MULTILINE)

        for m in import_re.finditer(source):
            pf.imports.append(m.group(0))

        for m in class_re.finditer(source):
            name = m.group(1)
            line = source[: m.start()].count("\n") + 1
            pf.symbols.append(
                ParsedSymbol(
                    name=name,
                    kind=SymbolKind.CLASS,
                    fqn=f"{pf.module_fqn}.{name}",
                    line_start=line,
                    line_end=line,
                )
            )

        for m in func_re.finditer(source):
            name = m.group(1)
            line = source[: m.start()].count("\n") + 1
            kind = SymbolKind.METHOD if m.group(0).startswith("    ") else SymbolKind.FUNCTION
            pf.symbols.append(
                ParsedSymbol(
                    name=name,
                    kind=kind,
                    fqn=f"{pf.module_fqn}.{name}",
                    line_start=line,
                    line_end=line,
                )
            )

        return pf
