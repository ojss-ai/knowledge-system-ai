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
