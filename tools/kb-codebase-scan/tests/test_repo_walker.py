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


def test_cache_file_lives_in_scanned_repo() -> None:
    repo_dir = make_temp_repo({"m.py": "def f(): pass"})
    walker = RepoWalker(ScanConfig(repo_path=repo_dir, languages=["python"]))
    for f in walker.iter_changed_files():
        walker.mark_scanned(f)
    walker.save_cache()
    assert (Path(repo_dir) / ".codebase_scan_cache.json").exists()
