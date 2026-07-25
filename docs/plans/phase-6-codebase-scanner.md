# Phase 6 — Codebase Scanner

**Goal:** Build the `kb-codebase-scan` CLI that walks a git repository, uses tree-sitter to parse Python and TypeScript source files, generates a knowledge graph of modules/classes/functions/calls, and upserts everything into the KB via the ingest-item API. Incremental scans diff by git commit hash.

**Architecture refs:** ADR-009 (tree-sitter, LanguageParser protocol, pluggable), ADR-004 (visibility)

**Required skills (read before any task):**
- `kb-conventions`
- `kb-tdd-workflow`
- `kb-ingestion-connectors` — LanguageParser protocol, diff by commit, confidence on call edges
- `kb-celery-jobs` (for the background dispatch path)

**Exit criteria:**
- [ ] All tasks checked
- [ ] `python -m pytest tools/kb-codebase-scan/tests/` green
- [ ] `ruff check tools/kb-codebase-scan/` clean
- [ ] `mypy --strict tools/kb-codebase-scan/` clean
- [ ] `kb-codebase-scan --dry-run` exits 0 on the backend/ directory itself
- [ ] Incremental scan: run twice on same repo → second run shows 0 new nodes
- [ ] Exit codes: 0=success, 1=scan error, 2=config error

---

## Task 1 — LanguageParser protocol + Python parser

**Files:**
- Create: `tools/kb-codebase-scan/language_parser.py`
- Create: `tools/kb-codebase-scan/python_parser.py`
- Create: `tools/kb-codebase-scan/tests/test_python_parser.py`
- Create: `tools/kb-codebase-scan/ruff.toml` ([plan-fix] mirrors `tools/kb-confluence-sync/ruff.toml` so the phase exit criterion `ruff check tools/kb-codebase-scan/` runs with project settings)

> **[plan-fix] block sync (Tasks 1–2):** code blocks below updated to match the committed code:
> `SymbolKind` inherits `enum.StrEnum` (ruff UP042; matches backend `NodeType`/`Visibility`); tree-sitter
> `Node`/`Iterator` type annotations added so `mypy --strict` passes (tree-sitter 0.26 ships py.typed);
> tests annotated `-> None` and unused imports dropped (ruff F401). Committed files are additionally
> `ruff format`-ed (slice spacing, call wrapping) — not re-transcribed here. tree-sitter >=0.22 API
> (`Language(tspython.language())`, `Parser(lang)`) works as written on tree-sitter 0.26.0.

### Steps

- [x] **1.1** Write the failing tests:

```python
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
```

- [x] **1.2** Create `language_parser.py`:

```python
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
    fqn: str                        # fully-qualified name: module.Class.method
    line_start: int
    line_end: int
    docstring: str = ""
    parent_fqn: str | None = None   # for methods: parent class FQN
    calls: list[str] = field(default_factory=list)   # names of called symbols
    confidence: float = 1.0         # 0.0–1.0; call edges are 0.7 (heuristic)


@dataclass
class ParsedFile:
    file_path: str                  # relative to repo root
    language: str
    module_fqn: str                 # e.g. "app.services.node_service"
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    error: str | None = None        # parse error if any


@runtime_checkable
class LanguageParser(Protocol):
    @property
    def extensions(self) -> list[str]:
        """File extensions handled by this parser (e.g. ['.py'])."""
        ...

    def parse(self, file_path: str, source: str) -> ParsedFile:
        """Parse source text and return structured symbols."""
        ...
```

- [x] **1.3** Create `python_parser.py` (uses tree-sitter):

```python
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
    """Convert file path to Python module FQN. 'app/services/node.py' → 'app.services.node'."""
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
                pf.imports.append(source[node.start_byte:node.end_byte].strip())

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
            name = source[name_node.start_byte:name_node.end_byte] if name_node else "Unknown"
            fqn = f"{pf.module_fqn}.{name}"
            doc = self._extract_docstring(node, source)
            sym = ParsedSymbol(
                name=name, kind=SymbolKind.CLASS, fqn=fqn,
                line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
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
            name = source[name_node.start_byte:name_node.end_byte] if name_node else "unknown"
            kind = SymbolKind.METHOD if class_fqn else SymbolKind.FUNCTION
            fqn = f"{parent_fqn or pf.module_fqn}.{name}"
            doc = self._extract_docstring(node, source)
            calls = self._extract_calls(node, source)
            sym = ParsedSymbol(
                name=name, kind=kind, fqn=fqn,
                line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
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
                raw = source[expr.start_byte:expr.end_byte]
                return raw.strip("\"'").strip()
        return ""

    def _extract_calls(self, func_node: Node, source: str) -> list[str]:
        calls: list[str] = []
        for node in self._iter_nodes(func_node):
            if node.type == "call":
                func_part = node.child_by_field_name("function")
                if func_part:
                    call_text = source[func_part.start_byte:func_part.end_byte]
                    # Get last segment (foo.bar → bar)
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
            line = source[:m.start()].count("\n") + 1
            pf.symbols.append(ParsedSymbol(
                name=name, kind=SymbolKind.CLASS,
                fqn=f"{pf.module_fqn}.{name}",
                line_start=line, line_end=line,
            ))

        for m in func_re.finditer(source):
            name = m.group(1)
            line = source[:m.start()].count("\n") + 1
            kind = SymbolKind.METHOD if m.group(0).startswith("    ") else SymbolKind.FUNCTION
            pf.symbols.append(ParsedSymbol(
                name=name, kind=kind,
                fqn=f"{pf.module_fqn}.{name}",
                line_start=line, line_end=line,
            ))

        return pf
```

- [x] **1.4** Create `tools/kb-codebase-scan/requirements.txt`:
```
requests>=2.31
tree-sitter>=0.21
tree-sitter-python>=0.21
tree-sitter-typescript>=0.21
python-dotenv>=1.0
gitpython>=3.1
```

- [x] **1.5** Run tests:
```bash
cd tools/kb-codebase-scan
pip install -r requirements.txt --break-system-packages
python -m pytest tests/test_python_parser.py -v
# Expected: 5 passed
```

- [x] **1.6** Commit:
```
feat(tools): LanguageParser protocol + PythonParser with tree-sitter
```

---

## Task 2 — TypeScript parser

**Files:**
- Create: `tools/kb-codebase-scan/typescript_parser.py`
- Create: `tools/kb-codebase-scan/tests/test_typescript_parser.py`

### Steps

- [x] **2.1** Write failing tests:

```python
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
```

- [x] **2.2** Create `typescript_parser.py`:

```python
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
            name = source[name_node.start_byte:name_node.end_byte] if name_node else "Unknown"
            fqn = f"{parent_fqn or pf.module_fqn}.{name}"
            sym = ParsedSymbol(
                name=name, kind=SymbolKind.CLASS, fqn=fqn,
                line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
            )
            pf.symbols.append(sym)
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    self._walk_node(child, pf, source, parent_fqn=fqn)

        elif node.type in ("function_declaration", "function"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte:name_node.end_byte]
                is_method = parent_fqn is not None and "." in parent_fqn
                kind = SymbolKind.METHOD if is_method else SymbolKind.FUNCTION
                fqn = f"{parent_fqn or pf.module_fqn}.{name}"
                sym = ParsedSymbol(
                    name=name, kind=kind, fqn=fqn,
                    line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
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
                        name = source[name_node.start_byte:name_node.end_byte]
                        fqn = f"{pf.module_fqn}.{name}"
                        sym = ParsedSymbol(
                            name=name, kind=SymbolKind.FUNCTION, fqn=fqn,
                            line_start=child.start_point[0] + 1, line_end=child.end_point[0] + 1,
                        )
                        pf.symbols.append(sym)
        else:
            for child in node.children:
                self._walk_node(child, pf, source, parent_fqn=parent_fqn)

    def _parse_regex_fallback(self, pf: ParsedFile, source: str) -> ParsedFile:
        for m in re.finditer(r"\bclass\s+(\w+)", source):
            name = m.group(1)
            line = source[:m.start()].count("\n") + 1
            pf.symbols.append(ParsedSymbol(
                name=name, kind=SymbolKind.CLASS, fqn=f"{pf.module_fqn}.{name}",
                line_start=line, line_end=line,
            ))
        for m in re.finditer(r"\bfunction\s+(\w+)\s*\(", source):
            name = m.group(1)
            line = source[:m.start()].count("\n") + 1
            pf.symbols.append(ParsedSymbol(
                name=name, kind=SymbolKind.FUNCTION, fqn=f"{pf.module_fqn}.{name}",
                line_start=line, line_end=line,
            ))
        for m in re.finditer(r"\bconst\s+(\w+)\s*=\s*(?:async\s*)?\(", source):
            name = m.group(1)
            line = source[:m.start()].count("\n") + 1
            pf.symbols.append(ParsedSymbol(
                name=name, kind=SymbolKind.FUNCTION, fqn=f"{pf.module_fqn}.{name}",
                line_start=line, line_end=line,
            ))
        return pf
```

- [x] **2.3** Run tests:
```bash
cd tools/kb-codebase-scan && python -m pytest tests/test_typescript_parser.py -v
# Expected: 4 passed
```

- [x] **2.4** Commit:
```
feat(tools): TypeScriptParser with tree-sitter (.ts, .tsx)
```

---

## Task 3 — Repo walker + incremental scan

**Files:**
- Create: `tools/kb-codebase-scan/repo_walker.py`
- Create: `tools/kb-codebase-scan/tests/test_repo_walker.py`

### Steps

- [x] **3.1** Write failing tests ([plan-fix] dropped unused `os`/`MagicMock` imports — ruff F401 — and added `-> None` annotations for `mypy --strict`, matching the other test files):

```python
# tools/kb-codebase-scan/tests/test_repo_walker.py
import tempfile
from pathlib import Path

from repo_walker import RepoWalker, ScanConfig


def make_temp_repo(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def test_walker_finds_python_files() -> None:
    repo_dir = make_temp_repo({
        "main.py": "def main(): pass",
        "lib/util.py": "def helper(): pass",
        "README.md": "# Doc",
    })
    config = ScanConfig(repo_path=repo_dir, languages=["python"])
    walker = RepoWalker(config)
    files = list(walker.iter_source_files())
    paths = [str(f) for f in files]
    assert any("main.py" in p for p in paths)
    assert any("util.py" in p for p in paths)
    assert not any(".md" in p for p in paths)


def test_walker_skips_excluded_dirs() -> None:
    repo_dir = make_temp_repo({
        "src/app.py": "x = 1",
        "node_modules/dep.py": "ignored",
        ".venv/lib.py": "ignored",
        "__pycache__/cache.py": "ignored",
    })
    config = ScanConfig(repo_path=repo_dir, languages=["python"])
    walker = RepoWalker(config)
    files = [str(f) for f in walker.iter_source_files()]
    assert any("app.py" in f for f in files)
    assert not any("node_modules" in f for f in files)
    assert not any(".venv" in f for f in files)
    assert not any("__pycache__" in f for f in files)


def test_incremental_skips_unchanged() -> None:
    """Files with same commit hash as cache should be skipped."""
    repo_dir = make_temp_repo({"mod.py": "def f(): pass"})
    config = ScanConfig(repo_path=repo_dir, languages=["python"])
    walker = RepoWalker(config)

    # Simulate: mod.py already scanned at current content hash
    rel_path = "mod.py"
    import hashlib
    content = Path(repo_dir, rel_path).read_text()
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    walker._hash_cache[rel_path] = content_hash

    files = [str(f) for f in walker.iter_changed_files()]
    assert not any("mod.py" in f for f in files)
```

- [x] **3.2** Create `repo_walker.py` ([plan-fix] `Generator` imported from `collections.abc` — ruff UP035 — and `_load_cache` assigns `json.load` to a typed local before returning, since `mypy --strict` rejects returning `Any`):

```python
# tools/kb-codebase-scan/repo_walker.py
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

_EXCLUDED_DIRS = frozenset({
    "node_modules", ".venv", "venv", "__pycache__", ".git",
    ".tox", "dist", "build", ".mypy_cache", ".pytest_cache",
    "coverage", ".next",
})

_LANGUAGE_EXTENSIONS: dict[str, list[str]] = {
    "python": [".py"],
    "typescript": [".ts", ".tsx"],
}


@dataclass
class ScanConfig:
    repo_path: str
    languages: list[str] = field(default_factory=lambda: ["python", "typescript"])
    max_file_size_kb: int = 500
    hash_cache_file: str = ".codebase_scan_cache.json"
    dry_run: bool = False
    kb_api_url: str = "http://localhost:8000"
    kb_token: str = ""
    visibility: str = "private"
    source_ref_prefix: str = ""    # e.g. "github.com/org/repo@main:"


class RepoWalker:
    def __init__(self, config: ScanConfig) -> None:
        self._config = config
        self._root = Path(config.repo_path).resolve()
        self._extensions: set[str] = set()
        for lang in config.languages:
            self._extensions.update(_LANGUAGE_EXTENSIONS.get(lang, []))
        self._hash_cache: dict[str, str] = self._load_cache()

    def _load_cache(self) -> dict[str, str]:
        try:
            with open(self._config.hash_cache_file) as f:
                cache: dict[str, str] = json.load(f)
                return cache
        except FileNotFoundError:
            return {}

    def save_cache(self) -> None:
        with open(self._config.hash_cache_file, "w") as f:
            json.dump(self._hash_cache, f)

    def _file_hash(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def iter_source_files(self) -> Generator[Path, None, None]:
        """Yield all source files under repo root, excluding excluded dirs."""
        for dirpath, dirnames, filenames in os.walk(self._root):
            # Prune excluded dirs in-place
            dirnames[:] = [
                d for d in dirnames
                if d not in _EXCLUDED_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                p = Path(dirpath) / filename
                if p.suffix in self._extensions:
                    if p.stat().st_size <= self._config.max_file_size_kb * 1024:
                        yield p

    def iter_changed_files(self) -> Generator[Path, None, None]:
        """Yield only files that have changed since the last scan."""
        for path in self.iter_source_files():
            rel = str(path.relative_to(self._root))
            current_hash = self._file_hash(path)
            if self._hash_cache.get(rel) != current_hash:
                yield path

    def mark_scanned(self, path: Path) -> None:
        rel = str(path.relative_to(self._root))
        self._hash_cache[rel] = self._file_hash(path)
```

- [x] **3.3** Run tests:
```bash
cd tools/kb-codebase-scan && python -m pytest tests/test_repo_walker.py -v
# Expected: 3 passed
```

- [x] **3.4** Commit:
```
feat(tools): RepoWalker with incremental scan via content-hash cache
```

---

## Task 4 — Scanner orchestrator + KB uploader

**Files:**
- Create: `tools/kb-codebase-scan/scanner.py`
- Create: `tools/kb-codebase-scan/tests/test_scanner.py`

### Steps

- [ ] **4.1** Write failing tests:

```python
# tools/kb-codebase-scan/tests/test_scanner.py
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch
from scanner import CodebaseScanner, ScanResult
from repo_walker import ScanConfig


def make_temp_repo(files):
    d = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def test_scan_produces_ingest_items():
    code = textwrap.dedent("""
        def alpha():
            beta()

        def beta():
            pass
    """)
    repo_dir = make_temp_repo({"lib.py": code})
    config = ScanConfig(repo_path=repo_dir, languages=["python"], dry_run=True)
    scanner = CodebaseScanner(config)
    items, edge_specs = scanner.collect()
    assert any(item.title == "alpha" or "alpha" in item.title for item in items)
    assert any(item.title == "beta" or "beta" in item.title for item in items)


def test_scan_dry_run_no_api_calls():
    """Dry run must not make HTTP calls."""
    repo_dir = make_temp_repo({"mod.py": "def foo(): pass"})
    config = ScanConfig(repo_path=repo_dir, languages=["python"], dry_run=True)
    scanner = CodebaseScanner(config)
    with patch("requests.Session.post") as mock_post:
        result = scanner.run()
        mock_post.assert_not_called()
    assert result.total >= 1


def test_incremental_second_scan_is_empty():
    """After full scan, a second scan on same files should yield 0 new items."""
    repo_dir = make_temp_repo({"mod.py": "def foo(): pass"})
    config = ScanConfig(repo_path=repo_dir, languages=["python"], dry_run=True)
    scanner = CodebaseScanner(config)
    result1 = scanner.run()
    # Second run — cache should prevent re-processing
    result2 = scanner.run()
    assert result2.new_items == 0
```

- [ ] **4.2** Create `scanner.py`:

```python
# tools/kb-codebase-scan/scanner.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests

from language_parser import ParsedFile, SymbolKind
from python_parser import PythonParser
from typescript_parser import TypeScriptParser
from repo_walker import RepoWalker, ScanConfig

# Import IngestItem-like dataclass (standalone, no backend dependency)
from dataclasses import dataclass as _dc

logger = logging.getLogger("kb-codebase-scan")


@_dc
class ScanIngestItem:
    source: str
    source_ref: str
    title: str
    body: str
    node_type: str = "code_symbol"
    visibility: str = "private"
    tags: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@_dc
class ScanEdgeSpec:
    source_ref: str
    target_ref: str
    label: str = "CALLS"
    props: dict = field(default_factory=dict)


@dataclass
class ScanResult:
    total: int = 0
    new_items: int = 0
    updated_items: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    api_calls: int = 0


class CodebaseScanner:
    """
    Orchestrates codebase scanning: walk → parse → upsert via KB API.
    """

    def __init__(self, config: ScanConfig) -> None:
        self._config = config
        self._walker = RepoWalker(config)
        self._parsers = {
            ext: parser
            for parser in [PythonParser(), TypeScriptParser()]
            for ext in parser.extensions
        }
        self._kb_session = requests.Session()
        self._kb_session.headers["Authorization"] = f"Bearer {config.kb_token}"
        self._kb_session.headers["Content-Type"] = "application/json"

    def _make_source_ref(self, file_path: Path, symbol_fqn: str | None = None) -> str:
        rel = str(file_path.relative_to(Path(self._config.repo_path).resolve()))
        prefix = self._config.source_ref_prefix
        base = f"{prefix}{rel}" if prefix else rel
        return f"{base}#{symbol_fqn}" if symbol_fqn else base

    def collect(self) -> tuple[list[ScanIngestItem], list[ScanEdgeSpec]]:
        """Parse all changed files and return items + edge specs (no API calls)."""
        items: list[ScanIngestItem] = []
        edge_specs: list[ScanEdgeSpec] = []
        fqn_to_ref: dict[str, str] = {}

        for file_path in self._walker.iter_changed_files():
            parser = self._parsers.get(file_path.suffix)
            if not parser:
                continue

            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
                parsed = parser.parse(str(file_path.relative_to(Path(self._config.repo_path).resolve())), source)
            except Exception as exc:
                logger.warning(f"Parse error in {file_path}: {exc}")
                continue

            # File-level node
            file_ref = self._make_source_ref(file_path)
            file_body = f"# {file_path.name}\n\nModule: `{parsed.module_fqn}`\n\n**Imports:**\n" + "\n".join(f"- `{i}`" for i in parsed.imports[:20])
            items.append(ScanIngestItem(
                source="codebase",
                source_ref=file_ref,
                title=file_path.name,
                body=file_body,
                node_type="code_file",
                tags=["code", parsed.language],
                meta={"language": parsed.language, "module_fqn": parsed.module_fqn},
            ))

            # Symbol-level nodes
            for sym in parsed.symbols:
                sym_ref = self._make_source_ref(file_path, sym.fqn)
                fqn_to_ref[sym.fqn] = sym_ref
                body = f"# `{sym.fqn}`\n\nType: {sym.kind.value}  Lines: {sym.line_start}–{sym.line_end}\n\n{sym.docstring}"
                items.append(ScanIngestItem(
                    source="codebase",
                    source_ref=sym_ref,
                    title=sym.name,
                    body=body.strip(),
                    node_type="code_symbol",
                    tags=["code", parsed.language, sym.kind.value],
                    meta={"fqn": sym.fqn, "kind": sym.kind.value, "language": parsed.language},
                ))

                # DEFINED_IN: symbol → file
                edge_specs.append(ScanEdgeSpec(
                    source_ref=sym_ref, target_ref=file_ref, label="DEFINED_IN",
                ))

        # CALLS edges (heuristic, confidence=0.7)
        for file_path in self._walker.iter_source_files():
            parser = self._parsers.get(file_path.suffix)
            if not parser:
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
                parsed = parser.parse(str(file_path.relative_to(Path(self._config.repo_path).resolve())), source)
            except Exception:
                continue
            for sym in parsed.symbols:
                caller_ref = self._make_source_ref(file_path, sym.fqn)
                for called_name in sym.calls:
                    # Find best matching target FQN
                    matches = [fqn for fqn in fqn_to_ref if fqn.endswith(f".{called_name}")]
                    for match_fqn in matches[:3]:  # top-3 candidates
                        edge_specs.append(ScanEdgeSpec(
                            source_ref=caller_ref,
                            target_ref=fqn_to_ref[match_fqn],
                            label="CALLS",
                            props={"confidence": 0.7},
                        ))

        return items, edge_specs

    def run(self) -> ScanResult:
        result = ScanResult()
        items, edge_specs = self.collect()
        result.total = len(items)

        if self._config.dry_run:
            logger.info(f"[DRY RUN] Would upsert {len(items)} items and {len(edge_specs)} edges")
            result.new_items = len(items)
            # Mark files as scanned (update hash cache) even in dry-run
            for file_path in self._walker.iter_changed_files():
                self._walker.mark_scanned(file_path)
            self._walker.save_cache()
            return result

        # Upsert items
        for item in items:
            try:
                r = self._kb_session.post(
                    f"{self._config.kb_api_url}/api/v1/uploads/ingest-item",
                    json={
                        "title": item.title,
                        "body": item.body,
                        "node_type": item.node_type,
                        "visibility": item.visibility,
                        "source": item.source,
                        "source_ref": item.source_ref,
                        "meta": item.meta,
                        "tags": item.tags,
                    },
                    timeout=30,
                )
                result.api_calls += 1
                r.raise_for_status()
                if r.status_code == 201:
                    result.new_items += 1
                else:
                    result.updated_items += 1
            except Exception as exc:
                logger.error(f"Failed to upsert {item.source_ref}: {exc}")
                result.failed_files += 1

        # Upsert edges via graph API
        for spec in edge_specs:
            try:
                r = self._kb_session.post(
                    f"{self._config.kb_api_url}/api/v1/edges",
                    json={
                        "source_ref": spec.source_ref,
                        "target_ref": spec.target_ref,
                        "label": spec.label,
                        "props": spec.props,
                    },
                    timeout=30,
                )
                result.api_calls += 1
                # 404 on unresolved refs is expected and benign
            except Exception:
                pass

        # Mark files as scanned
        for file_path in self._walker.iter_changed_files():
            self._walker.mark_scanned(file_path)
        self._walker.save_cache()

        return result
```

- [ ] **4.3** Run tests:
```bash
cd tools/kb-codebase-scan && python -m pytest tests/test_scanner.py -v
# Expected: 3 passed
```

- [ ] **4.4** Commit:
```
feat(tools): CodebaseScanner orchestrator — parse, upsert, CALLS edges with confidence=0.7
```

---

## Task 5 — CLI entrypoint

**Files:**
- Create: `tools/kb-codebase-scan/__main__.py`
- Create: `tools/kb-codebase-scan/pyproject.toml`
- Create: `tools/kb-codebase-scan/tests/test_cli.py`

### Steps

- [ ] **5.1** Write failing tests:

```python
# tools/kb-codebase-scan/tests/test_cli.py
import subprocess, sys


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "__main__", *args],
        capture_output=True, text=True,
        cwd="tools/kb-codebase-scan",
    )


def test_help():
    r = run_cli("--help")
    assert r.returncode == 0


def test_missing_repo_exits_2():
    r = run_cli("scan", "--repo", "/nonexistent/path/xyz")
    assert r.returncode == 2


def test_dry_run_on_self(tmp_path):
    """Scan the scanner's own directory in dry-run mode."""
    import os
    r = subprocess.run(
        [sys.executable, "-m", "__main__", "scan",
         "--repo", "tools/kb-codebase-scan",
         "--dry-run", "--language", "python"],
        capture_output=True, text=True,
        env={**os.environ, "KB_API_TOKEN": "fake"},
    )
    assert r.returncode == 0
```

- [ ] **5.2** Create `__main__.py`:

```python
# tools/kb-codebase-scan/__main__.py
"""
kb-codebase-scan — Codebase knowledge graph generator.

Usage:
    python -m kb_codebase_scan scan --repo /path/to/repo [--dry-run]

Environment variables:
    KB_API_URL      http://localhost:8000
    KB_API_TOKEN    Service token
    SCAN_LANGUAGES  python,typescript  (default)

Exit codes: 0=success, 1=scan error, 2=config error
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="kb-codebase-scan")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="Scan a repository")
    scan.add_argument("--repo", required=True, help="Path to repository root")
    scan.add_argument("--dry-run", action="store_true")
    scan.add_argument("--language", dest="languages", action="append",
                      help="Languages to scan (python, typescript). Repeatable.")
    scan.add_argument("--visibility", default="private")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    repo_path = Path(args.repo).resolve()
    if not repo_path.exists():
        print(f"ERROR: Repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(2)

    kb_token = os.environ.get("KB_API_TOKEN", "")
    kb_url = os.environ.get("KB_API_URL", "http://localhost:8000")

    if not args.dry_run and not kb_token:
        print("ERROR: KB_API_TOKEN is required (or use --dry-run)", file=sys.stderr)
        sys.exit(2)

    languages = args.languages or os.environ.get("SCAN_LANGUAGES", "python,typescript").split(",")

    from repo_walker import ScanConfig
    from scanner import CodebaseScanner

    config = ScanConfig(
        repo_path=str(repo_path),
        languages=languages,
        dry_run=args.dry_run,
        kb_api_url=kb_url,
        kb_token=kb_token,
        visibility=args.visibility,
    )

    scanner = CodebaseScanner(config)
    try:
        result = scanner.run()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        import json
        print(json.dumps({
            "total": result.total, "new": result.new_items,
            "updated": result.updated_items, "failed": result.failed_files,
        }))
    else:
        print(f"Scan complete: {result.new_items} new, {result.updated_items} updated, {result.failed_files} failed")

    sys.exit(1 if result.failed_files > 0 else 0)


if __name__ == "__main__":
    main()
```

- [ ] **5.3** Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "kb-codebase-scan"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "requests>=2.31",
    "tree-sitter>=0.21",
    "tree-sitter-python>=0.21",
    "tree-sitter-typescript>=0.21",
    "gitpython>=3.1",
    "python-dotenv>=1.0",
]

[project.scripts]
kb-codebase-scan = "kb_codebase_scan.__main__:main"
```

- [ ] **5.4** Run tests and full gate:
```bash
cd tools/kb-codebase-scan
python -m pytest tests/ -v
ruff check .
mypy --strict language_parser.py python_parser.py typescript_parser.py repo_walker.py scanner.py

# Dry-run on backend/ itself:
KB_API_TOKEN=fake python __main__.py scan --repo ../../backend --dry-run --language python
# Expected: exit 0, prints "Scan complete: N new, ..."
```

- [ ] **5.5** Commit:
```
feat(tools): kb-codebase-scan CLI — scan, dry-run, --json output, exit codes 0/1/2
```

---

## Phase 6 exit gate

```bash
# Tool tests
cd tools/kb-codebase-scan
python -m pytest tests/ -v            # all green
ruff check .                          # clean
mypy --strict *.py

# Incremental scan idempotency:
python -m pytest tests/test_scanner.py::test_incremental_second_scan_is_empty -v

# Dry-run on real code:
KB_API_TOKEN=fake python __main__.py scan --repo ../../backend --dry-run --language python --json
# Expected: exit 0, JSON output with total > 0

# Exit code tests:
python __main__.py scan --repo /nonexistent; echo "exit: $?"  # → 2
```

Update `docs/plans/README.md` — Phase 6 Status → `Done`.
