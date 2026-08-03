"""kb-codebase-scan — Codebase knowledge graph generator.

Usage:
    python __main__.py scan --repo /path/to/repo [--dry-run]
    kb-codebase-scan scan --repo /path/to/repo [--dry-run]   (pip-installed)

Environment variables (or .env file):
    KB_API_URL      http://localhost:8000
    KB_API_TOKEN    Service token from /api/v1/tokens
    SCAN_LANGUAGES  python,typescript  (default)

Exit codes:
    0  Success
    1  Scan error (failed batches or crash)
    2  Configuration error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from repo_walker import ScanConfig
from scanner import CodebaseScanner

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional — env vars still work
    pass
else:
    load_dotenv()


def build_config(args: argparse.Namespace) -> ScanConfig:
    repo_path = Path(args.repo).resolve()
    if not repo_path.exists():
        print(f"ERROR: Repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(2)

    kb_token = os.environ.get("KB_API_TOKEN", "")
    kb_url = os.environ.get("KB_API_URL", "http://localhost:8000")
    if not args.dry_run and not kb_token:
        print("ERROR: KB_API_TOKEN is required (or use --dry-run)", file=sys.stderr)
        sys.exit(2)

    languages: list[str] = args.languages or [
        s.strip() for s in os.environ.get("SCAN_LANGUAGES", "python,typescript").split(",")
    ]

    return ScanConfig(
        repo_path=str(repo_path),
        languages=languages,
        dry_run=args.dry_run,
        kb_api_url=kb_url,
        kb_token=kb_token,
        visibility=args.visibility,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kb-codebase-scan",
        description="Scan a code repository into the Knowledge Base",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="Scan a repository")
    scan.add_argument("--repo", required=True, help="Path to repository root")
    scan.add_argument("--dry-run", action="store_true", help="Preview without writing")
    scan.add_argument(
        "--language",
        dest="languages",
        action="append",
        help="Languages to scan (python, typescript). Repeatable.",
    )
    scan.add_argument("--visibility", default="private")
    scan.add_argument("--json", action="store_true", help="Output results as JSON")
    scan.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    config = build_config(args)
    scanner = CodebaseScanner(config)

    try:
        result = scanner.run()
    except Exception as exc:
        print(f"ERROR: Scan failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(
            json.dumps(
                {
                    "total": result.total,
                    "new": result.new_items,
                    "updated": result.updated_items,
                    "failed": result.failed_files,
                }
            )
        )
    else:
        print(
            f"Scan complete: {result.new_items} new, {result.updated_items} updated, "
            f"{result.failed_files} failed"
        )

    sys.exit(1 if result.failed_files > 0 else 0)


if __name__ == "__main__":
    main()
