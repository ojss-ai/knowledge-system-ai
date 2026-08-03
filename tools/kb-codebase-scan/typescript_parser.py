# tools/kb-codebase-scan/typescript_parser.py
from __future__ import annotations

import re
from pathlib import Path

from language_parser import ParsedFile, ParsedSymbol, SymbolKind

try:
    import tree_sitter_typescript as tsts
    from tree_sitter import Language, Node, Parser

    _TS_LANG = Language(tsts.language_typescript())
    _HAS_TREE_SITTER = True
except ImportError:
    _HAS_TREE_SITTER = False


def _fqn_from_path(file_path: str) -> str:
    p = Path(file_path)
    return ".".join(p.with_suffix("").parts)


class TypeScriptParser:
    @property
    def extensions(self) -> list[str]:
        return [".ts", ".tsx"]

    def parse(self, file_path: str, source: str) -> ParsedFile:
        module_fqn = _fqn_from_path(file_path)
        pf = ParsedFile(file_path=file_path, language="typescript", module_fqn=module_fqn)

        if _HAS_TREE_SITTER:
            return self._parse_tree_sitter(pf, source)
        return self._parse_regex_fallback(pf, source)

    def _parse_tree_sitter(self, pf: ParsedFile, source: str) -> ParsedFile:
        parser = Parser(_TS_LANG)
        tree = parser.parse(source.encode())
        self._walk_node(tree.root_node, pf, source, parent_fqn=None)
        return pf

    def _walk_node(self, node: Node, pf: ParsedFile, source: str, parent_fqn: str | None) -> None:
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            name = source[name_node.start_byte : name_node.end_byte] if name_node else "Unknown"
            fqn = f"{parent_fqn or pf.module_fqn}.{name}"
            sym = ParsedSymbol(
                name=name,
                kind=SymbolKind.CLASS,
                fqn=fqn,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
            pf.symbols.append(sym)
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    self._walk_node(child, pf, source, parent_fqn=fqn)

        elif node.type in ("function_declaration", "function"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte : name_node.end_byte]
                is_method = parent_fqn is not None and "." in parent_fqn
                kind = SymbolKind.METHOD if is_method else SymbolKind.FUNCTION
                fqn = f"{parent_fqn or pf.module_fqn}.{name}"
                sym = ParsedSymbol(
                    name=name,
                    kind=kind,
                    fqn=fqn,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    parent_fqn=parent_fqn,
                )
                pf.symbols.append(sym)

        elif node.type == "lexical_declaration":
            # const foo = (...) => ...
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    val_node = child.child_by_field_name("value")
                    if name_node and val_node and val_node.type in ("arrow_function", "function"):
                        name = source[name_node.start_byte : name_node.end_byte]
                        fqn = f"{pf.module_fqn}.{name}"
                        sym = ParsedSymbol(
                            name=name,
                            kind=SymbolKind.FUNCTION,
                            fqn=fqn,
                            line_start=child.start_point[0] + 1,
                            line_end=child.end_point[0] + 1,
                        )
                        pf.symbols.append(sym)
        else:
            for child in node.children:
                self._walk_node(child, pf, source, parent_fqn=parent_fqn)

    def _parse_regex_fallback(self, pf: ParsedFile, source: str) -> ParsedFile:
        for m in re.finditer(r"\bclass\s+(\w+)", source):
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
        for m in re.finditer(r"\bfunction\s+(\w+)\s*\(", source):
            name = m.group(1)
            line = source[: m.start()].count("\n") + 1
            pf.symbols.append(
                ParsedSymbol(
                    name=name,
                    kind=SymbolKind.FUNCTION,
                    fqn=f"{pf.module_fqn}.{name}",
                    line_start=line,
                    line_end=line,
                )
            )
        for m in re.finditer(r"\bconst\s+(\w+)\s*=\s*(?:async\s*)?\(", source):
            name = m.group(1)
            line = source[: m.start()].count("\n") + 1
            pf.symbols.append(
                ParsedSymbol(
                    name=name,
                    kind=SymbolKind.FUNCTION,
                    fqn=f"{pf.module_fqn}.{name}",
                    line_start=line,
                    line_end=line,
                )
            )
        return pf
