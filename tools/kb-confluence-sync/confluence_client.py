# tools/kb-confluence-sync/confluence_client.py
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass
class ConfluencePage:
    page_id: str
    title: str
    version: int
    space_key: str
    web_url: str
    body_storage: str | None = None       # XHTML storage format
    parent_id: str | None = None
    labels: list[str] = field(default_factory=list)


class ConfluenceClient:
    """
    Thin REST client for Confluence Cloud and Server.
    Supports personal access tokens (Cloud) and basic auth (Server).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        email: str | None = None,        # required for Cloud (email:token basic auth)
        verify_ssl: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.verify = verify_ssl

        # Auth: Cloud uses email:token Basic, Server uses Bearer token
        if email:
            creds = base64.b64encode(f"{email}:{token}".encode()).decode()
            self._session.headers["Authorization"] = f"Basic {creds}"
        else:
            self._session.headers["Authorization"] = f"Bearer {token}"

        self._session.headers["Accept"] = "application/json"
        self._session.headers["Content-Type"] = "application/json"

        # Retry on transient failures
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def list_pages(self, space_key: str, limit: int = 50) -> list[ConfluencePage]:
        """List all pages in a space (handles pagination)."""
        pages: list[ConfluencePage] = []
        start = 0

        while True:
            params: dict[str, str | int] = {
                "spaceKey": space_key,
                "type": "page",
                "limit": limit,
                "start": start,
                "expand": "version,ancestors",
            }
            r = self._session.get(f"{self._base}/rest/api/content", params=params)
            r.raise_for_status()
            data = r.json()

            for item in data.get("results", []):
                pages.append(
                    ConfluencePage(
                        page_id=item["id"],
                        title=item["title"],
                        version=item["version"]["number"],
                        space_key=space_key,
                        web_url=item.get("_links", {}).get("webui", ""),
                        parent_id=(
                            item["ancestors"][-1]["id"]
                            if item.get("ancestors")
                            else None
                        ),
                    )
                )

            if not data.get("_links", {}).get("next"):
                break
            start += limit

        return pages

    def get_page(self, page_id: str) -> ConfluencePage:
        """Fetch a single page with full storage-format body."""
        r = self._session.get(
            f"{self._base}/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,space,ancestors,metadata.labels"},
        )
        r.raise_for_status()
        item = r.json()

        labels = [
            lbl["name"]
            for lbl in item.get("metadata", {}).get("labels", {}).get("results", [])
        ]

        return ConfluencePage(
            page_id=item["id"],
            title=item["title"],
            version=item["version"]["number"],
            space_key=item.get("space", {}).get("key", ""),
            web_url=item.get("_links", {}).get("webui", ""),
            body_storage=item.get("body", {}).get("storage", {}).get("value", ""),
            parent_id=(
                item["ancestors"][-1]["id"] if item.get("ancestors") else None
            ),
            labels=labels,
        )
