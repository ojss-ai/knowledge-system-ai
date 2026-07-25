# tools/kb-codebase-scan/tests/test_cli.py
"""CLI entrypoint tests (Task 5).

[plan-fix] vs the 5.1 block (mirrors tools/kb-confluence-sync/tests/test_cli.py):
- Flat-module layout (Tasks 1-4): there is no kb_codebase_scan package to
  `python -m`; the CLI runs as `python __main__.py` (matches the exit-gate
  invocation). `cwd`/`--repo` were relative to the repo root, but pytest runs
  from the tool dir — resolved from __file__.
- Env is scrubbed of KB_* so the exit-code tests are deterministic regardless
  of the developer's shell; dry-run runs WITHOUT a token (that is the behavior
  the flag promises — the plan block exported a fake token it never needed).
- test_dry_run_on_self scans a tmp copy of the tool's own sources so the hash
  cache never lands in the working tree.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
# `python` from PATH first: sys.executable may be a loader-wrapped binary that
# cannot be exec'd directly (sandbox py312); PATH carries the working wrapper.
PYTHON = shutil.which("python") or sys.executable


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("KB_")}
    if env:
        base_env.update(env)
    return subprocess.run(
        [PYTHON, "__main__.py", *args],
        capture_output=True,
        text=True,
        cwd=TOOL_DIR,
        env=base_env,
    )


def copy_own_sources(tmp_path: Path) -> Path:
    """The scanner's own directory, minus caches — dry-run on real code."""
    repo = tmp_path / "self"
    repo.mkdir()
    for src in TOOL_DIR.glob("*.py"):
        shutil.copy(src, repo / src.name)
    return repo


def test_help_exits_0() -> None:
    r = run_cli("--help")
    assert r.returncode == 0
    assert "usage" in r.stdout.lower()


def test_missing_repo_exits_2() -> None:
    r = run_cli("scan", "--repo", "/nonexistent/path/xyz")
    assert r.returncode == 2
    assert "does not exist" in r.stderr


def test_missing_token_without_dry_run_exits_2(tmp_path: Path) -> None:
    r = run_cli("scan", "--repo", str(tmp_path))
    assert r.returncode == 2
    assert "KB_API_TOKEN" in r.stderr


def test_dry_run_on_self(tmp_path: Path) -> None:
    """Scan the scanner's own sources in dry-run mode — no token required."""
    repo = copy_own_sources(tmp_path)
    r = run_cli("scan", "--repo", str(repo), "--dry-run", "--language", "python")
    assert r.returncode == 0, r.stderr
    assert "Scan complete:" in r.stdout


def test_dry_run_json_output(tmp_path: Path) -> None:
    repo = copy_own_sources(tmp_path)
    r = run_cli("scan", "--repo", str(repo), "--dry-run", "--language", "python", "--json")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["total"] > 0
    assert set(data) == {"total", "new", "updated", "failed"}
