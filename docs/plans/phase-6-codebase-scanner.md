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

## Task 4a — Backend: `POST /api/v1/uploads/ingest-batch` + DB-fallback edge resolution

> **[re-plan, human-approved 2026-07-25]** The original Task 4 posted edges to `/api/v1/edges` —
> that endpoint takes node UUIDs (`EdgeCreate.source_id/target_id`), has no score/props, and is
> owner-gated interactive mutation, not ingestion. Scanner edges instead ride the existing
> `KnowledgeIngestor` two-pass via a new batch endpoint. `DEFINED_IN` is dead: canonical label is
> `DEFINES` (file → symbol). Deliberate scope cuts (Phase 7 candidates): no PARENT_OF hierarchy,
> no IMPORTS edges, batch endpoint is bounded-synchronous (no run tracking).

> **[plan-fix] block sync (4a.3):** `_resolve_ref`'s DB probe binds a fresh `found` variable instead
> of reassigning `node` — reassigning the dict.get-inferred variable makes mypy --strict resolve
> `scalar()`'s overload to Any (`no-any-return`). Committed test file is additionally `ruff format`-ed
> (edge-dict lines collapsed) — not re-transcribed here.

> **[review-fix 4.R.1] blocks 4a.1/4a.3/4a.6 updated in place:** `resolve_edges` gained
> `fallback_source` (the DB probe is source-pinned; skipped when None), `IngestBatchIn` gained an
> optional `fallback_source` field, and the endpoint derives it from the items' single distinct
> source. Three new source-scope service tests and three new API tests live in the committed test
> files (named in 4.R.1) — not re-transcribed here.

**Files:**
- Modify: `backend/app/services/ingest/base.py` (stats counters, `resolve_edges` db fallback)
- Modify: `backend/app/api/v1/uploads.py` (schemas + endpoint)
- Modify: `backend/tests/services/ingest/test_ingest_base.py`
- Create: `backend/tests/api/test_ingest_batch_api.py`

### Steps

- [x] **4a.1** Write failing service tests — append to `backend/tests/services/ingest/test_ingest_base.py` (reuses its `_graph_recorder`, `db`, `make_user` fixtures; note `_graph_recorder`'s `fake_merge` already accepts `score=None` — extend the tuple it records with `score` so 4a's last test can assert on it: `calls.append(("edge", str(source_id), str(target_id), label, created_by, score))`, and update the existing `("edge", ...)` assertions in this file to compare `c[:5]` or unpack/ignore the extra element):

```python
async def test_resolve_edges_db_fallback_resolves_committed_ref(db, make_user, monkeypatch):
    """A ref absent from this batch resolves against an already-persisted row."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_fb@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    first = KnowledgeIngestor(db, viewer)
    tgt = await first.upsert(
        IngestItem(source="codebase", source_ref="r/a.py#a.beta", title="beta", body="b")
    )
    await db.flush()

    second = KnowledgeIngestor(db, viewer)  # fresh: empty _ref_to_node
    src = await second.upsert(
        IngestItem(source="codebase", source_ref="r/b.py#b.alpha", title="alpha", body="a")
    )
    second.add_edge_spec(
        EdgeSpec(
            source_ref="r/b.py#b.alpha",
            target_ref="r/a.py#a.beta",
            label="CALLS",
            props={"score": 0.7},
        )
    )
    dangling = await second.resolve_edges(db_fallback=True, fallback_source="codebase")
    assert dangling == 0
    await ns.run_pending_graph_ops(db)
    assert ("edge", str(src.id), str(tgt.id), "CALLS", "ingest", 0.7) in calls


async def test_resolve_edges_db_fallback_never_crosses_owners(db, make_user, monkeypatch):
    """Fallback is pinned to (viewer visibility, owner) — another user's node never resolves."""
    _graph_recorder(monkeypatch)
    other = await make_user(email="ing_fb_other@test.com")
    other_viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    await KnowledgeIngestor(db, other_viewer).upsert(
        IngestItem(source="codebase", source_ref="shared.py#x", title="x", body="x")
    )
    await db.flush()

    owner = await make_user(email="ing_fb_me@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ing = KnowledgeIngestor(db, viewer)
    await ing.upsert(IngestItem(source="codebase", source_ref="mine.py#m", title="m", body="m"))
    ing.add_edge_spec(EdgeSpec(source_ref="mine.py#m", target_ref="shared.py#x", label="CALLS"))
    dangling = await ing.resolve_edges(db_fallback=True, fallback_source="codebase")
    assert dangling == 1  # dangling, not another owner's node


async def test_upsert_stats_created_updated_skipped(db, make_user):
    owner = await make_user(email="ing_stats@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ing = KnowledgeIngestor(db, viewer)
    await ing.upsert(IngestItem(source="codebase", source_ref="s.py", title="S", body="1"))
    await ing.upsert(IngestItem(source="codebase", source_ref="s.py", title="S", body="1"))
    await ing.upsert(IngestItem(source="codebase", source_ref="s.py", title="S", body="2"))
    assert ing.stats == {"created": 1, "skipped": 1, "updated": 1}
```

- [x] **4a.2** Run them — MUST fail (`TypeError: unexpected keyword argument 'db_fallback'` / no `stats`):
```bash
cd backend && python -m pytest tests/services/ingest/test_ingest_base.py -v   # RED
```

- [x] **4a.3** Amend `backend/app/services/ingest/base.py`. In `__init__`, after `self._edge_specs`:

```python
        # created/updated/skipped counts for this ingestor's lifetime — the
        # batch endpoint reports them; workers may ignore them.
        self.stats: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0}
```

In `upsert`, add one counter per branch: in the `existing is not None` branch, `self.stats["updated"] += 1` right after the `ns.update_node(...)` call and `self.stats["skipped"] += 1` in the unchanged-hash branch; in the create branch, `self.stats["created"] += 1` after `ns.create_node(...)`. Then replace `resolve_edges` entirely and add `_resolve_ref`:

```python
    async def resolve_edges(
        self, *, db_fallback: bool = False, fallback_source: str | None = None
    ) -> int:
        """
        Pass 2: resolve queued EdgeSpecs to node IDs and QUEUE the graph MERGEs
        for post-commit run_pending_graph_ops() — never awaited in-transaction
        (ADR-011). Unresolvable refs are skipped and counted (dangling links are
        expected in batch imports, not errors). Returns the dangling count.

        db_fallback=True additionally resolves refs not seen by THIS ingestor
        against persisted rows — same visibility clause + owner pin as upsert's
        probe (kb-visibility-filter rule 1), plus a source pin [review-fix
        4.R.1]: source_ref is only unique WITHIN a source, so the probe filters
        on fallback_source and is SKIPPED (ref counts as dangling) when
        fallback_source is None — never probe unscoped, or a same-owner md doc
        and code file sharing a source_ref would mislink. The in-memory
        _ref_to_node map is intentionally NOT source-pinned: within one
        ingestor all items belong to one logical import. Used by the HTTP batch
        path, where CALLS targets may have been ingested in an earlier request
        or scan run.
        """
        dangling = 0
        for spec in self._edge_specs:
            src_node = await self._resolve_ref(spec.source_ref, db_fallback, fallback_source)
            tgt_node = await self._resolve_ref(spec.target_ref, db_fallback, fallback_source)
            if src_node is None or tgt_node is None:
                dangling += 1
                continue
            score = spec.props.get("score")
            ns.queue_graph_op(self._db, partial(gs.upsert_vertex, src_node))
            ns.queue_graph_op(self._db, partial(gs.upsert_vertex, tgt_node))
            ns.queue_graph_op(
                self._db,
                partial(
                    gs.merge_edge,
                    src_node.id,
                    tgt_node.id,
                    spec.label,
                    created_by=str(spec.props.get("created_by", "ingest")),
                    score=float(score) if score is not None else None,
                ),
            )
        self._edge_specs.clear()
        return dangling

    async def _resolve_ref(
        self, ref: str, db_fallback: bool, fallback_source: str | None
    ) -> KnowledgeNode | None:
        node = self._ref_to_node.get(ref)
        if node is not None or not db_fallback or fallback_source is None:
            return node
        # Owner pin for the same reason as upsert's probe: the visibility
        # clause alone would match another owner's public node with this ref.
        # Source pin [review-fix 4.R.1]: source_ref is only unique within a
        # source — probing without it could hijack a same-owner ref collision.
        # [plan-fix] fresh binding (not `node = ...`): reassigning the
        # dict.get-inferred variable makes mypy --strict resolve scalar()'s
        # overload to Any → no-any-return.
        found = await self._db.scalar(
            select(KnowledgeNode).where(
                visible_nodes_clause(self._viewer),
                KnowledgeNode.owner_id == self._viewer.user_id,
                KnowledgeNode.source == fallback_source,
                KnowledgeNode.source_ref == ref,
            )
        )
        if found is not None:
            self._ref_to_node[ref] = found  # memoize: CALLS fan-in hits the same target
        return found
```

- [x] **4a.4** Verify GREEN, including the untouched md-worker path:
```bash
cd backend && python -m pytest tests/services/ingest/ tests/workers/test_ingest_md.py -v
```

- [x] **4a.5** Write failing API tests — create `backend/tests/api/test_ingest_batch_api.py`:

```python
"""ingest-batch API tests (Task 4a). Auth/idempotency mirror test_ingest_item_api."""

from httpx import AsyncClient


def _item(ref: str, title: str, body: str = "b") -> dict:
    return {
        "title": title,
        "body": body,
        "node_type": "code_symbol",
        "source": "codebase",
        "source_ref": ref,
        "tags": ["code"],
    }


async def test_batch_creates_nodes_and_queues_edges(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [_item("r/f.py", "f.py"), _item("r/f.py#f.alpha", "alpha")],
            "edges": [
                {"source_ref": "r/f.py", "target_ref": "r/f.py#f.alpha", "label": "DEFINES"}
            ],
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["created"] == 2 and data["edges_queued"] == 1 and data["edges_dangling"] == 0


async def test_batch_idempotent_second_run_creates_nothing(client: AsyncClient, auth_headers):
    payload = {"items": [_item("r/idem.py", "idem.py")], "edges": []}
    await client.post("/api/v1/uploads/ingest-batch", json=payload, headers=auth_headers)
    r2 = await client.post("/api/v1/uploads/ingest-batch", json=payload, headers=auth_headers)
    assert r2.json()["created"] == 0 and r2.json()["skipped"] == 1


async def test_batch_resolves_ref_from_previous_request(client: AsyncClient, auth_headers):
    await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={"items": [_item("r/prev.py#p.f", "f")], "edges": []},
    )
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [_item("r/next.py#n.g", "g")],
            "edges": [
                {
                    "source_ref": "r/next.py#n.g",
                    "target_ref": "r/prev.py#p.f",
                    "label": "CALLS",
                    "confidence": 0.7,
                }
            ],
        },
    )
    assert r.json()["edges_queued"] == 1 and r.json()["edges_dangling"] == 0


async def test_batch_counts_dangling_edges(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [_item("r/only.py", "only.py")],
            "edges": [
                {"source_ref": "r/only.py", "target_ref": "r/ghost.py", "label": "DEFINES"}
            ],
        },
    )
    assert r.status_code == 200 and r.json()["edges_dangling"] == 1


async def test_batch_unknown_label_is_422(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [],
            "edges": [{"source_ref": "a", "target_ref": "b", "label": "DEFINED_IN"}],
        },
    )
    assert r.status_code == 422


async def test_batch_unauthenticated_is_401(client: AsyncClient):
    r = await client.post("/api/v1/uploads/ingest-batch", json={"items": [], "edges": []})
    assert r.status_code == 401
```

```bash
cd backend && python -m pytest tests/api/test_ingest_batch_api.py -v   # RED: 404s
```

- [x] **4a.6** Add to `backend/app/api/v1/uploads.py`. Imports: extend the existing `from app.services.ingest.base import ...` line with `EdgeSpec`, add `field_validator` to the pydantic import and `from app.services.graph_service import ALLOWED_EDGE_LABELS`. Below `IngestItemIn`:

```python
class EdgeSpecIn(BaseModel):
    """Ref-addressed edge for batch ingestion. `confidence` (ADR-009 call edges)
    maps to merge_edge's score. Label check mirrors EdgeCreate: the label is
    interpolated into Cypher, only the fixed vocabulary may pass (422)."""

    source_ref: str = Field(..., min_length=1)
    target_ref: str = Field(..., min_length=1)
    label: str = "LINKS_TO"
    confidence: float | None = Field(None, ge=0.0, le=1.0)

    @field_validator("label")
    @classmethod
    def _label_allowed(cls, v: str) -> str:
        if v not in ALLOWED_EDGE_LABELS:
            raise ValueError("unknown edge label")
        return v


class IngestBatchIn(BaseModel):
    # Bounded sync upsert (same tradeoff as ingest-item; the run-tracked async
    # path is the Phase 7 upgrade). Caps keep one request under the kb-api
    # long-work threshold; clients chunk.
    items: list[IngestItemIn] = Field(default_factory=list, max_length=200)
    edges: list[EdgeSpecIn] = Field(default_factory=list, max_length=2000)
    # [review-fix 4.R.1] source scope for DB-fallback edge resolution:
    # source_ref is only unique WITHIN a source. Needed for edge-only batches
    # (the scanner posts all items first, then edge-only batches); when omitted
    # it is derived from the items' single distinct source, else the fallback
    # is skipped entirely — never probe unscoped.
    fallback_source: str | None = Field(None, min_length=1)


class IngestBatchOut(BaseModel):
    created: int
    updated: int
    skipped: int
    edges_queued: int
    edges_dangling: int
```

And the endpoint, after `ingest_single_item`:

```python
@router.post(
    "/ingest-batch",
    response_model=IngestBatchOut,
    summary="Upsert a batch of knowledge nodes and ref-addressed edges",
    operation_id="ingestBatch",
)
async def ingest_batch(
    payload: IngestBatchIn,
    viewer: Viewer = Depends(_require_ingest_scope),
    db: AsyncSession = Depends(get_db),
) -> IngestBatchOut:
    """Batch upsert for connectors (codebase scanner). Two-pass edge resolution
    with DB fallback: refs may point at nodes from earlier batches or scans.
    Dangling refs are counted, never errors (kb-ingestion-connectors)."""
    ingestor = KnowledgeIngestor(db, viewer)
    for item_in in payload.items:
        await ingestor.upsert(
            IngestItem(
                source=item_in.source or "api",
                source_ref=item_in.source_ref or str(uuid.uuid4()),
                title=item_in.title,
                body=item_in.body,
                node_type=item_in.node_type,
                visibility=item_in.visibility,
                tags=item_in.tags,
                meta=item_in.meta,
            )
        )
    for e in payload.edges:
        props: dict = {"score": e.confidence} if e.confidence is not None else {}
        ingestor.add_edge_spec(
            EdgeSpec(source_ref=e.source_ref, target_ref=e.target_ref, label=e.label, props=props)
        )
    # [review-fix 4.R.1] pin the DB fallback to one source: explicit field
    # wins; else the items' single distinct source (after the `or "api"`
    # defaulting); else None → resolve_edges skips the probe (dangling).
    sources = {item_in.source or "api" for item_in in payload.items}
    derived = next(iter(sources)) if len(sources) == 1 else None
    fallback_source = payload.fallback_source or derived
    dangling = await ingestor.resolve_edges(db_fallback=True, fallback_source=fallback_source)
    await db.commit()
    await ns.run_pending_graph_ops(db)  # Neo4j strictly after PG commit (ADR-011)
    return IngestBatchOut(
        created=ingestor.stats["created"],
        updated=ingestor.stats["updated"],
        skipped=ingestor.stats["skipped"],
        edges_queued=len(payload.edges) - dangling,
        edges_dangling=dangling,
    )
```

- [x] **4a.7** Full gate:
```bash
cd backend && python -m pytest tests/api/test_ingest_batch_api.py tests/services/ingest/ -v  # green
ruff check app tests && mypy app/services app/schemas
```

- [x] **4a.8** Commit:
```
feat(api): POST /uploads/ingest-batch — ref-addressed edges with DB-fallback resolution
```

---

## Task 4b — Scanner orchestrator posting to ingest-batch

**Files:**
- Modify: `tools/kb-codebase-scan/repo_walker.py` (+ test) — per-repo cache path
- Create: `tools/kb-codebase-scan/scanner.py`
- Create: `tools/kb-codebase-scan/tests/test_scanner.py`
- Modify: `tools/kb-codebase-scan/requirements.txt` (append `types-requests>=2.31` for mypy --strict)

### Steps

- [x] **4b.1** RED — append to `tools/kb-codebase-scan/tests/test_repo_walker.py` (the cwd-relative default cache breaks test isolation and cross-repo scans):

```python
def test_cache_file_lives_in_scanned_repo() -> None:
    repo_dir = make_temp_repo({"m.py": "def f(): pass"})
    walker = RepoWalker(ScanConfig(repo_path=repo_dir, languages=["python"]))
    for f in walker.iter_changed_files():
        walker.mark_scanned(f)
    walker.save_cache()
    assert (Path(repo_dir) / ".codebase_scan_cache.json").exists()
```

- [x] **4b.2** GREEN — in `repo_walker.py` add a `_cache_path` helper and use it in `_load_cache`/`save_cache` (relative `hash_cache_file` resolves under the repo root; absolute paths still honored). `_load_cache` also self-heals on garbage, mirroring sync_engine 5.R.4:

```python
    def _cache_path(self) -> Path:
        p = Path(self._config.hash_cache_file)
        return p if p.is_absolute() else self._root / p

    def _load_cache(self) -> dict[str, str]:
        try:
            with open(self._cache_path()) as f:
                cache: dict[str, str] = json.load(f)
                return cache
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            return {}  # unreadable cache → full re-scan; upserts are idempotent

    def save_cache(self) -> None:
        with open(self._cache_path(), "w") as f:
            json.dump(self._hash_cache, f)
```
```bash
cd tools/kb-codebase-scan && python -m pytest tests/test_repo_walker.py -v   # 4 passed
```

- [x] **4b.3** RED — create `tools/kb-codebase-scan/tests/test_scanner.py`:

```python
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from repo_walker import ScanConfig
from scanner import CodebaseScanner


def make_temp_repo(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


CALLS_CODE = textwrap.dedent("""
    def alpha():
        beta()

    def beta():
        pass
""")


def test_collect_produces_file_and_symbol_items() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    items, _ = CodebaseScanner(
        ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    ).collect()
    titles = [i.title for i in items]
    assert "lib.py" in titles and "alpha" in titles and "beta" in titles
    assert {i.node_type for i in items} == {"code_file", "code_symbol"}
    repo_tag = f"codebase:{Path(repo).name}"
    assert all(repo_tag in i.tags for i in items)


def test_defines_edge_is_file_to_symbol() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    _, edges = CodebaseScanner(
        ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    ).collect()
    defines = [e for e in edges if e.label == "DEFINES"]
    assert defines and all(e.source_ref == "lib.py" and "#" in e.target_ref for e in defines)


def test_calls_edge_carries_confidence() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    _, edges = CodebaseScanner(
        ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    ).collect()
    calls = [e for e in edges if e.label == "CALLS"]
    assert any(
        e.source_ref.endswith("#lib.alpha")
        and e.target_ref.endswith("#lib.beta")
        and e.confidence == 0.7
        for e in calls
    )


def test_dry_run_makes_no_api_calls() -> None:
    repo = make_temp_repo({"mod.py": "def foo(): pass"})
    scanner = CodebaseScanner(ScanConfig(repo_path=repo, languages=["python"], dry_run=True))
    with patch("requests.Session.post") as mock_post:
        result = scanner.run()
        mock_post.assert_not_called()
    assert result.total >= 1


def test_incremental_second_scan_is_empty() -> None:
    repo = make_temp_repo({"mod.py": "def foo(): pass"})
    config = ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    CodebaseScanner(config).run()
    assert CodebaseScanner(config).run().new_items == 0


def test_run_posts_batches_to_ingest_batch() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    config = ScanConfig(
        repo_path=repo, languages=["python"], kb_token="tok", kb_api_url="http://kb.local"
    )
    scanner = CodebaseScanner(config)
    ok = MagicMock(status_code=200)
    ok.json.return_value = {
        "created": 3,
        "updated": 0,
        "skipped": 0,
        "edges_queued": 3,
        "edges_dangling": 0,
    }
    with patch("requests.Session.post", return_value=ok) as mock_post:
        result = scanner.run()
    urls = [c.args[0] for c in mock_post.call_args_list]
    assert urls and all(u == "http://kb.local/api/v1/uploads/ingest-batch" for u in urls)
    payloads: list[dict[str, Any]] = [c.kwargs["json"] for c in mock_post.call_args_list]
    assert set(payloads[0]) == {"items", "edges", "fallback_source"}
    # [4.R.1] edge-only batches have no items to derive a source from — every
    # payload pins the DB-fallback scope explicitly.
    assert all(p["fallback_source"] == "codebase" for p in payloads)
    assert result.new_items == 3 and result.failed_batches == 0


def test_run_stops_posting_after_first_failed_batch() -> None:
    """[4.R.2] One failed POST aborts the run — no requests wasted on batches
    that will be re-sent next run anyway (cache is not saved on failure)."""
    repo = make_temp_repo({"a.py": "def a(): pass", "b.py": "def b(): pass"})
    config = ScanConfig(
        repo_path=repo, languages=["python"], kb_token="tok", kb_api_url="http://kb.local"
    )
    scanner = CodebaseScanner(config)
    with (
        patch("scanner._BATCH_ITEMS", 1),
        patch("requests.Session.post", side_effect=RuntimeError("boom")) as mock_post,
    ):
        result = scanner.run()
    assert mock_post.call_count == 1  # 4 items + 1 edge batch without the early abort
    assert result.failed_batches == 1 and result.failed_files == 1


def test_changed_caller_still_links_to_unchanged_callee() -> None:
    """Symbol table spans ALL files; items only re-emit for changed ones."""
    repo = make_temp_repo({"a.py": "def alpha():\n    beta()\n", "b.py": "def beta(): pass"})
    config = ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    CodebaseScanner(config).run()  # everything cached
    Path(repo, "a.py").write_text("def alpha():\n    beta()\n    beta()\n")
    items, edges = CodebaseScanner(config).collect()
    assert all(i.source_ref.startswith("a.py") for i in items)  # only a.py re-emits
    assert any(e.label == "CALLS" and e.target_ref.endswith("#b.beta") for e in edges)
```
```bash
cd tools/kb-codebase-scan && python -m pytest tests/test_scanner.py -v   # RED: ImportError
```

- [x] **4b.4** GREEN — create `tools/kb-codebase-scan/scanner.py`:

```python
# tools/kb-codebase-scan/scanner.py
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from language_parser import LanguageParser, ParsedFile
from python_parser import PythonParser
from repo_walker import RepoWalker, ScanConfig
from typescript_parser import TypeScriptParser

logger = logging.getLogger("kb-codebase-scan")

_CALL_CONFIDENCE = 0.7   # heuristic static resolution (ADR-009)
_MAX_CALL_TARGETS = 3    # cap fan-out per ambiguous call name
_BATCH_ITEMS = 200       # server cap (IngestBatchIn)
_BATCH_EDGES = 2000
_TIMEOUT_S = 30


@dataclass
class ScanIngestItem:
    source: str
    source_ref: str
    title: str
    body: str
    node_type: str = "code_symbol"
    visibility: str = "private"
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanEdgeSpec:
    source_ref: str
    target_ref: str
    label: str = "CALLS"
    confidence: float | None = None


@dataclass
class ScanResult:
    total: int = 0
    new_items: int = 0
    updated_items: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    failed_batches: int = 0
    edges_sent: int = 0
    edges_dangling: int = 0
    api_calls: int = 0


class CodebaseScanner:
    """Walk → parse → POST /api/v1/uploads/ingest-batch (items + ref edges).

    All symbols across the repo feed the fqn table; only CHANGED files emit
    items/edges. Edges to unchanged targets resolve server-side (DB fallback);
    first-run refs resolve in-batch. DEFINES is file → symbol (canonical)."""

    def __init__(self, config: ScanConfig) -> None:
        self._config = config
        self._root = Path(config.repo_path).resolve()
        self._repo_tag = f"codebase:{self._root.name}"
        self._walker = RepoWalker(config)
        parsers: list[LanguageParser] = [PythonParser(), TypeScriptParser()]
        self._parsers: dict[str, LanguageParser] = {
            ext: p for p in parsers for ext in p.extensions
        }
        self._kb_session = requests.Session()
        self._kb_session.headers["Authorization"] = f"Bearer {config.kb_token}"
        self._kb_session.headers["Content-Type"] = "application/json"
        self._changed: list[Path] = []

    def _ref(self, rel_path: str, fqn: str | None = None) -> str:
        base = f"{self._config.source_ref_prefix}{rel_path}"
        return f"{base}#{fqn}" if fqn else base

    def _parse(self, path: Path) -> ParsedFile | None:
        parser = self._parsers.get(path.suffix)
        if parser is None:
            return None
        rel = str(path.relative_to(self._root)).replace("\\", "/")
        try:
            return parser.parse(rel, path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # error-tolerant by design (ADR-009)
            logger.warning(f"Parse error in {path}: {exc}")
            return None

    def collect(self) -> tuple[list[ScanIngestItem], list[ScanEdgeSpec]]:
        """Parse the repo once; emit items + edges for changed files only."""
        self._changed = list(self._walker.iter_changed_files())
        changed_rel = {str(p.relative_to(self._root)).replace("\\", "/") for p in self._changed}
        parsed_files = [pf for p in self._walker.iter_source_files() if (pf := self._parse(p))]

        fqn_to_ref: dict[str, str] = {}
        for pf in parsed_files:
            for sym in pf.symbols:
                fqn_to_ref[sym.fqn] = self._ref(pf.file_path, sym.fqn)

        items: list[ScanIngestItem] = []
        edges: list[ScanEdgeSpec] = []
        for pf in parsed_files:
            if pf.file_path not in changed_rel:
                continue
            file_ref = self._ref(pf.file_path)
            imports_md = "\n".join(f"- `{i}`" for i in pf.imports[:20])
            items.append(
                ScanIngestItem(
                    source="codebase",
                    source_ref=file_ref,
                    title=Path(pf.file_path).name,
                    body=(
                        f"# {Path(pf.file_path).name}\n\nModule: `{pf.module_fqn}`\n\n"
                        f"**Imports:**\n{imports_md}"
                    ),
                    node_type="code_file",
                    visibility=self._config.visibility,
                    tags=["code", pf.language, self._repo_tag],
                    meta={
                        "language": pf.language,
                        "module_fqn": pf.module_fqn,
                        "file_path": pf.file_path,
                    },
                )
            )
            for sym in pf.symbols:
                sym_ref = self._ref(pf.file_path, sym.fqn)
                items.append(
                    ScanIngestItem(
                        source="codebase",
                        source_ref=sym_ref,
                        title=sym.name,
                        body=(
                            f"# `{sym.fqn}`\n\nType: {sym.kind.value}  "
                            f"Lines: {sym.line_start}-{sym.line_end}\n\n{sym.docstring}"
                        ).strip(),
                        node_type="code_symbol",
                        visibility=self._config.visibility,
                        tags=["code", pf.language, sym.kind.value, self._repo_tag],
                        meta={
                            "fqn": sym.fqn,
                            "kind": sym.kind.value,
                            "language": pf.language,
                            "file_path": pf.file_path,
                            "line_start": sym.line_start,
                        },
                    )
                )
                edges.append(
                    ScanEdgeSpec(source_ref=file_ref, target_ref=sym_ref, label="DEFINES")
                )
                for called in sym.calls:
                    matches = [f for f in fqn_to_ref if f.endswith(f".{called}")]
                    for match in matches[:_MAX_CALL_TARGETS]:
                        if fqn_to_ref[match] != sym_ref:
                            edges.append(
                                ScanEdgeSpec(
                                    source_ref=sym_ref,
                                    target_ref=fqn_to_ref[match],
                                    label="CALLS",
                                    confidence=_CALL_CONFIDENCE,
                                )
                            )
        return items, edges

    def _post_batch(
        self,
        items: list[ScanIngestItem],
        edges: list[ScanEdgeSpec],
        result: ScanResult,
    ) -> None:
        r = self._kb_session.post(
            f"{self._config.kb_api_url}/api/v1/uploads/ingest-batch",
            json={
                "items": [asdict(i) for i in items],
                "edges": [asdict(e) for e in edges],
                # [4.R.1] source_ref is only unique WITHIN a source: pin the
                # server's DB-fallback edge resolution to this scanner's source.
                # Edge-only batches carry no items to derive it from — without
                # the pin the server skips the fallback and every edge dangles.
                "fallback_source": "codebase",
            },
            timeout=_TIMEOUT_S,
        )
        result.api_calls += 1
        r.raise_for_status()
        data = r.json()
        if items:  # item counters only from item batches (edge batches create nothing)
            result.new_items += int(data["created"])
            result.updated_items += int(data["updated"])
        if edges:
            result.edges_sent += int(data["edges_queued"])
            result.edges_dangling += int(data["edges_dangling"])

    def run(self) -> ScanResult:
        result = ScanResult()
        items, edges = self.collect()
        result.total = len(items)

        if self._config.dry_run:
            logger.info(f"[DRY RUN] Would upsert {len(items)} items, {len(edges)} edges")
            result.new_items = len(items)
        else:
            failed = False
            # Items first (all batches), edges after: every ref is committed
            # before any edge resolution — dangling only for genuinely absent refs.
            # [4.R.2] first failure aborts both loops: the cache is not saved on
            # failure, so every remaining batch is re-sent next run anyway —
            # keep the requests.
            for start in range(0, len(items), _BATCH_ITEMS):
                try:
                    self._post_batch(items[start : start + _BATCH_ITEMS], [], result)
                except Exception as exc:
                    logger.error(f"Batch upsert failed: {exc}")
                    result.failed_batches += 1
                    failed = True
                    break
            if not failed:
                for start in range(0, len(edges), _BATCH_EDGES):
                    try:
                        self._post_batch([], edges[start : start + _BATCH_EDGES], result)
                    except Exception as exc:
                        logger.error(f"Edge batch failed: {exc}")
                        result.failed_batches += 1
                        failed = True
                        break
            if failed:
                result.failed_files = result.failed_batches  # Task 5 exit-code signal
                return result  # cache NOT saved → next run re-sends (idempotent upserts)

        for path in self._changed:
            self._walker.mark_scanned(path)
        self._walker.save_cache()
        return result
```

> **[plan-fix]** `_post_batch` originally accumulated all four counters unconditionally; since `run()` posts item-only then edge-only batches, that double-counted `created` against the test's single mock response (and is wrong-shaped for real responses too). Counters are now guarded by batch kind. Also: the scanner test file defines 7 tests, not 8 as the gate comment said.

- [x] **4b.5** Append `types-requests>=2.31` to `tools/kb-codebase-scan/requirements.txt`, then the full gate:
```bash
cd tools/kb-codebase-scan
python -m pytest tests/ -v          # all green (parser 9, walker 4, scanner 7)
ruff check .                        # clean
mypy --strict language_parser.py python_parser.py typescript_parser.py repo_walker.py scanner.py
```

- [x] **4b.6** Commit:
```
feat(tools): CodebaseScanner — ingest-batch upserts, DEFINES file→symbol, CALLS confidence=0.7
```

---

## Task 4.R — Review fixes (2026-07-25)

- [x] **4.R.1 IMPORTANT — DB-fallback edge resolution is now source-scoped.**
  `_resolve_ref`'s DB probe matched on (visibility, owner, source_ref) but not `source`;
  the contract says source_ref is unique WITHIN a source, so a same-owner ref collision
  across sources (md doc vs code file with the same source_ref) would mislink edges.
  `resolve_edges(*, db_fallback: bool = False, fallback_source: str | None = None)` now adds
  `KnowledgeNode.source == fallback_source` to the probe; with `db_fallback=True` but
  `fallback_source=None` the probe is SKIPPED and the ref counts as dangling — never probe
  unscoped. The in-memory `_ref_to_node` map stays source-agnostic (within one ingestor all
  items are one logical import — noted in the docstring). `ingest_batch` derives
  fallback_source = the items' single distinct `source` (after the `or "api"` defaulting),
  else None.
  **[plan-fix] deviation from the approved note "Scanner is unaffected":** the scanner posts
  all item batches first, then EDGE-ONLY batches (4b.4 `run()`), so items-derivation alone
  would skip the fallback on every scanner edge batch and every cross-batch edge would
  dangle silently. Resolution: optional `IngestBatchIn.fallback_source` (explicit field wins
  over derivation) and the scanner sends `"fallback_source": "codebase"` in every payload.
  Tests (RED first): service — `test_resolve_edges_db_fallback_is_source_scoped`,
  `test_resolve_edges_db_fallback_matching_source_resolves`,
  `test_resolve_edges_db_fallback_without_source_never_probes` (the two existing fallback
  tests now pass `fallback_source="codebase"`); API —
  `test_batch_fallback_never_crosses_sources`,
  `test_batch_edge_only_with_fallback_source_resolves`,
  `test_batch_edge_only_without_fallback_source_is_dangling`; tool —
  `test_run_posts_batches_to_ingest_batch` asserts the payload pin.

- [x] **4.R.2 NIT — scanner aborts batch posting after the first failure.** `run()` kept
  POSTing every remaining item/edge batch after a failure even though the cache is never
  saved on a failed run (all batches are re-sent next run anyway). The first failure now
  `break`s the item loop and skips the edge loop entirely. Test:
  `test_run_stops_posting_after_first_failed_batch` (RED: 5 requests → GREEN: 1).

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
