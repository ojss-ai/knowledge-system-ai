"""kb-confluence-sync — Incremental Confluence to Knowledge Base sync tool.

Usage:
    python __main__.py sync --space SPACE_KEY [--dry-run]
    kb-confluence-sync sync --space SPACE_KEY [--dry-run]   (pip-installed)

Environment variables (or .env file):
    CONFLUENCE_URL         https://your-domain.atlassian.net/wiki
    CONFLUENCE_TOKEN       Personal Access Token
    CONFLUENCE_EMAIL       Your email (required for Cloud)
    CONFLUENCE_SPACES      Comma-separated space keys (alternative to --space)
    KB_API_URL             http://localhost:8000
    KB_API_TOKEN           Service token from /api/v1/tokens

Exit codes:
    0  Success
    1  Sync error (partial failure)
    2  Configuration error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from sync_engine import SyncConfig, SyncEngine

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional — env vars still work
    pass
else:
    load_dotenv()


def build_config(args: argparse.Namespace) -> SyncConfig:
    url = os.environ.get("CONFLUENCE_URL")
    token = os.environ.get("CONFLUENCE_TOKEN")
    kb_url = os.environ.get("KB_API_URL", "http://localhost:8000")
    kb_token = os.environ.get("KB_API_TOKEN")

    if not url or not token or not kb_token:
        print(
            "ERROR: CONFLUENCE_URL, CONFLUENCE_TOKEN, and KB_API_TOKEN are required",
            file=sys.stderr,
        )
        sys.exit(2)

    space_keys: list[str] = []
    if args.space:
        space_keys = [s.strip() for s in args.space.split(",")]
    elif os.environ.get("CONFLUENCE_SPACES"):
        space_keys = [s.strip() for s in os.environ["CONFLUENCE_SPACES"].split(",")]

    if not space_keys:
        print("ERROR: Specify --space SPACE_KEY or set CONFLUENCE_SPACES", file=sys.stderr)
        sys.exit(2)

    return SyncConfig(
        confluence_url=url,
        confluence_token=token,
        confluence_email=os.environ.get("CONFLUENCE_EMAIL"),
        space_keys=space_keys,
        kb_api_url=kb_url,
        kb_token=kb_token,
        dry_run=args.dry_run,
        visibility=args.visibility,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kb-confluence-sync",
        description="Sync Confluence spaces into the Knowledge Base",
    )
    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="Sync one or more Confluence spaces")
    sync_parser.add_argument("--space", help="Comma-separated space keys to sync")
    sync_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    sync_parser.add_argument(
        "--visibility", default="private", choices=["private", "public", "shared"]
    )
    sync_parser.add_argument("--json", action="store_true", help="Output results as JSON")
    sync_parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    config = build_config(args)
    engine = SyncEngine(config)

    try:
        results = engine.sync_all()
    except Exception as exc:
        print(f"ERROR: Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)

    any_failed = False
    for space, result in results.items():
        if args.json:
            print(
                json.dumps(
                    {
                        "space": space,
                        "created": result.created,
                        "updated": result.updated,
                        "skipped": result.skipped,
                        "failed": result.failed,
                    }
                )
            )
        else:
            print(
                f"[{space}] created={result.created} updated={result.updated} "
                f"skipped={result.skipped} failed={result.failed}"
            )
        if result.failed > 0:
            any_failed = True

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
