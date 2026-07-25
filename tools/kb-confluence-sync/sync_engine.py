# tools/kb-confluence-sync/sync_engine.py
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from confluence_client import ConfluenceClient, ConfluencePage
from xhtml_to_md import convert_storage_to_md

logger = logging.getLogger("kb-confluence-sync")


@dataclass
class SyncConfig:
    confluence_url: str
    confluence_token: str
    space_keys: list[str]
    kb_api_url: str
    kb_token: str
    confluence_email: str | None = None
    dry_run: bool = False
    visibility: str = "private"
    version_cache_file: str = ".confluence_versions.json"


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    api_calls: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped + self.failed


class SyncEngine:
    """
    Incremental Confluence → KB sync engine.

    Version cache: stores {space_key:page_id → last_synced_version} in a local JSON file.
    Pages whose version matches the cache are skipped (no API call).
    """

    def __init__(self, config: SyncConfig) -> None:
        self._config = config
        self._client = ConfluenceClient(
            base_url=config.confluence_url,
            token=config.confluence_token,
            email=config.confluence_email,
        )
        self._version_cache: dict[str, int] = self._load_cache()
        self._kb_session = requests.Session()
        self._kb_session.headers["Authorization"] = f"Bearer {config.kb_token}"
        self._kb_session.headers["Content-Type"] = "application/json"

    def _load_cache(self) -> dict[str, int]:
        try:
            with open(self._config.version_cache_file) as f:
                cache: dict[str, int] = json.load(f)
                return cache
        except FileNotFoundError:
            return {}  # first run — silent
        except (json.JSONDecodeError, OSError) as exc:
            # [review-fix 5.R.4] self-healing: a truncated/garbage/unreadable
            # cache must not brick the tool. Worst case every page re-syncs —
            # the ingest-item endpoint is an idempotent upsert by source_ref.
            logger.warning(
                f"Version cache {self._config.version_cache_file!r} unreadable "
                f"({exc}); starting with an empty cache — all pages will re-sync"
            )
            return {}

    def _save_cache(self) -> None:
        with open(self._config.version_cache_file, "w") as f:
            json.dump(self._version_cache, f)

    def _cache_key(self, space_key: str, page_id: str) -> str:
        return f"{space_key}:{page_id}"

    def sync_space(self, space_key: str) -> SyncResult:
        result = SyncResult()
        pages = self._client.list_pages(space_key)
        logger.info(f"Found {len(pages)} pages in space {space_key}")

        for page in pages:
            cache_key = self._cache_key(space_key, page.page_id)
            if self._version_cache.get(cache_key) == page.version:
                logger.debug(f"Skipping {page.title} (version {page.version} unchanged)")
                result.skipped += 1
                continue

            if self._config.dry_run:
                # [review-fix 5.R.5] a cached page (older version) is a
                # would-update; an uncached one a would-create. Best-effort from
                # local knowledge only — dry run never asks the KB API.
                if cache_key in self._version_cache:
                    logger.info(f"[DRY RUN] Would update: {page.title} (v{page.version})")
                    result.updated += 1
                else:
                    logger.info(f"[DRY RUN] Would create: {page.title} (v{page.version})")
                    result.created += 1
                continue

            try:
                full_page = self._client.get_page(page.page_id)
                self._upsert_page(full_page, result)
                self._version_cache[cache_key] = page.version
            except Exception as exc:
                logger.error(f"Failed to sync {page.title}: {exc}")
                result.failed += 1

        if not self._config.dry_run:
            self._save_cache()

        return result

    def _upsert_page(self, page: ConfluencePage, result: SyncResult) -> None:
        md, meta = convert_storage_to_md(page.body_storage or "", return_meta=True)

        source_ref = f"confluence:{page.space_key}:{page.page_id}"
        payload = {
            "title": page.title,
            "body": md,
            "node_type": "confluence_page",
            "visibility": self._config.visibility,
            "source": "confluence",
            "source_ref": source_ref,
            "meta": {
                "confluence_page_id": page.page_id,
                "confluence_version": page.version,
                "confluence_space": page.space_key,
                "web_url": page.web_url,
                **meta,
            },
            "tags": page.labels,
        }

        # Upsert: the ingest-item endpoint creates (201) or updates (200) by source_ref
        ingest_r = self._kb_session.post(
            f"{self._config.kb_api_url}/api/v1/uploads/ingest-item",
            json=payload,
        )
        result.api_calls += 1
        ingest_r.raise_for_status()

        if ingest_r.status_code == 201:
            result.created += 1
        else:
            result.updated += 1

    def sync_all(self) -> dict[str, SyncResult]:
        results: dict[str, SyncResult] = {}
        for space_key in self._config.space_keys:
            logger.info(f"Syncing space: {space_key}")
            results[space_key] = self.sync_space(space_key)
        return results
