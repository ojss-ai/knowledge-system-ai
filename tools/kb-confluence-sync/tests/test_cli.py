"""CLI entrypoint tests (Task 5).

[plan-fix] vs the 5.1 block:
- The tool is a flat-module layout (Tasks 1-3: `from confluence_client import ...`),
  so there is no `kb_confluence_sync` package to `python -m`; the CLI runs as
  `python __main__.py` (matches the exit-gate invocation). `cwd` was also relative
  to the repo root, but pytest runs from the tool dir — resolved from __file__.
- Env is scrubbed of CONFLUENCE_*/KB_* so test_missing_config_exits_2 is
  deterministic regardless of the developer's shell.
- Added test_dry_run_exits_0_against_mocked_confluence — the phase exit criterion
  "`--dry-run` exits 0 against a mocked Confluence server" (plan 5.5 pointed the
  CLI at example.atlassian.net, a real network call that fails offline).
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parents[1]
# `python` from PATH first: sys.executable may be a loader-wrapped binary that
# cannot be exec'd directly (sandbox py312); PATH carries the working wrapper.
PYTHON = shutil.which("python") or sys.executable


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("CONFLUENCE_", "KB_"))}
    if env:
        base_env.update(env)
    return subprocess.run(
        [PYTHON, "__main__.py", *args],
        capture_output=True,
        text=True,
        cwd=TOOL_DIR,
        env=base_env,
    )


def test_help_exits_0() -> None:
    r = run_cli("--help")
    assert r.returncode == 0
    assert "usage" in r.stdout.lower()


def test_missing_config_exits_2() -> None:
    r = run_cli("sync", "--space", "TS")
    assert r.returncode == 2


class _ConfluenceStub(BaseHTTPRequestHandler):
    """Minimal mocked Confluence server: one page in every space, no pagination."""

    def do_GET(self) -> None:
        payload = {
            "results": [{"id": "1", "title": "Stub Page", "version": {"number": 1}}],
            "_links": {},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep test output pristine


def test_dry_run_exits_0_against_mocked_confluence() -> None:
    server = HTTPServer(("127.0.0.1", 0), _ConfluenceStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        r = run_cli(
            "sync",
            "--space",
            "TS",
            "--dry-run",
            env={
                "CONFLUENCE_URL": f"http://127.0.0.1:{server.server_port}",
                "CONFLUENCE_TOKEN": "fake",
                "KB_API_TOKEN": "fake",
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert r.returncode == 0, r.stderr
    assert "created=1" in r.stdout
