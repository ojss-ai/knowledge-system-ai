"""Markdown zip importer (kb-ingestion-connectors, md_importer section).

Connector layer only: parses a zip of ``.md`` files into IngestItems and
wikilink EdgeSpecs. Persistence is owned by KnowledgeIngestor — never here.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import structlog

from app.services.ingest.base import EdgeSpec, IngestItem

logger = structlog.get_logger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
# [ \t] not \s: \s matches newlines, so "#   \n\nbody" would swallow the blank
# line and title itself "body" (review CRITICAL — empty-H1 fallback broken).
_HEADING_RE = re.compile(r"^#[ \t]+(.+)", re.MULTILINE)


def extract_wikilinks(body: str) -> list[str]:
    return _WIKILINK_RE.findall(body)


def _title_from_body_or_filename(body: str, filename: str) -> str:
    """Extract first H1 heading as title, fall back to filename stem.

    A whitespace-only H1 ("#   ") must fall through to the filename —
    nodes must never be created with an empty title (review CRITICAL).
    """
    m = _HEADING_RE.search(body)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return Path(filename).stem.replace("-", " ").replace("_", " ").title()


def parse_zip(
    zip_bytes: bytes,
    source: str = "md_upload",
) -> tuple[list[IngestItem], list[EdgeSpec]]:
    """
    Parse a zip archive of Markdown files.
    Returns (items, edge_specs) ready for KnowledgeIngestor.
    """
    items: list[IngestItem] = []
    edge_specs: list[EdgeSpec] = []
    title_to_ref: dict[str, str] = {}  # title → source_ref (for wikilink resolution)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        md_files = [
            n for n in zf.namelist() if n.lower().endswith(".md") and not n.startswith("__MACOSX")
        ]

        for name in md_files:
            try:
                body = zf.read(name).decode("utf-8", errors="replace")
            except Exception as exc:
                # Skip unreadable members, but never silently (review IMPORTANT)
                logger.warning("md_import_unreadable_member", member=name, error=str(exc))
                continue

            title = _title_from_body_or_filename(body, name)
            item = IngestItem(
                source=source,
                source_ref=name,
                title=title,
                body=body,
            )
            items.append(item)
            # First-wins on duplicate titles: deterministic wikilink resolution
            # (review CRITICAL — last-wins silently rebound links to whichever
            # file happened to be processed last).
            if title in title_to_ref:
                logger.warning(
                    "md_import_duplicate_title",
                    title=title,
                    kept=title_to_ref[title],
                    ignored=name,
                )
            else:
                title_to_ref[title] = name

    # Second pass: resolve wikilinks to source_refs
    for item in items:
        for linked_title in extract_wikilinks(item.body):
            target_ref = title_to_ref.get(linked_title)
            if target_ref and target_ref != item.source_ref:
                edge_specs.append(
                    EdgeSpec(
                        source_ref=item.source_ref,
                        target_ref=target_ref,
                        label="LINKS_TO",
                        props={"created_by": "wikilink"},
                    )
                )

    return items, edge_specs
