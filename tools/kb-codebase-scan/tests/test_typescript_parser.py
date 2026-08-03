# tools/kb-codebase-scan/tests/test_typescript_parser.py
import textwrap

from language_parser import LanguageParser, SymbolKind
from typescript_parser import TypeScriptParser


def test_parses_functions() -> None:
    code = "function greet(name: string): string { return `Hello ${name}`; }"
    parser = TypeScriptParser()
    result = parser.parse("greet.ts", code)
    funcs = [s for s in result.symbols if s.kind == SymbolKind.FUNCTION]
    assert any(s.name == "greet" for s in funcs)


def test_parses_classes() -> None:
    code = textwrap.dedent("""
        class GraphCanvas {
            constructor(private container: HTMLElement) {}
            render(): void { this.draw(); }
        }
    """)
    parser = TypeScriptParser()
    result = parser.parse("GraphCanvas.ts", code)
    classes = [s for s in result.symbols if s.kind == SymbolKind.CLASS]
    assert any(s.name == "GraphCanvas" for s in classes)


def test_parses_arrow_functions() -> None:
    code = "const add = (a: number, b: number): number => a + b;"
    parser = TypeScriptParser()
    result = parser.parse("math.ts", code)
    funcs = [s for s in result.symbols if s.kind == SymbolKind.FUNCTION]
    assert any(s.name == "add" for s in funcs)


def test_satisfies_protocol() -> None:
    parser = TypeScriptParser()
    assert isinstance(parser, LanguageParser)
    assert ".ts" in parser.extensions
