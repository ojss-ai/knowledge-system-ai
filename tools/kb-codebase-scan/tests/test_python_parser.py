# tools/kb-codebase-scan/tests/test_python_parser.py
import textwrap

from language_parser import SymbolKind
from python_parser import PythonParser


def test_parses_functions() -> None:
    code = textwrap.dedent("""
        def greet(name: str) -> str:
            return f"Hello, {name}"
    """)
    parser = PythonParser()
    result = parser.parse("test.py", code)
    syms = [s for s in result.symbols if s.kind == SymbolKind.FUNCTION]
    assert any(s.name == "greet" for s in syms)


def test_parses_classes() -> None:
    code = textwrap.dedent("""
        class MyClass:
            def method(self):
                pass
    """)
    parser = PythonParser()
    result = parser.parse("mymod.py", code)
    classes = [s for s in result.symbols if s.kind == SymbolKind.CLASS]
    assert any(s.name == "MyClass" for s in classes)


def test_parses_imports() -> None:
    code = "import os\nfrom pathlib import Path\n"
    parser = PythonParser()
    result = parser.parse("imp.py", code)
    assert "os" in result.imports or any("os" in imp for imp in result.imports)


def test_detects_function_calls() -> None:
    code = textwrap.dedent("""
        def main():
            greet("world")
            print("done")
    """)
    parser = PythonParser()
    result = parser.parse("calls.py", code)
    calls = [s for s in result.symbols if s.kind == SymbolKind.FUNCTION and s.name == "main"]
    assert len(calls) == 1
    # calls in main body
    assert "greet" in calls[0].calls or "print" in calls[0].calls


def test_parse_result_satisfies_protocol() -> None:
    from language_parser import LanguageParser

    parser = PythonParser()
    assert isinstance(parser, LanguageParser)
