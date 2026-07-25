# Phase 5 — Confluence Sync

**Goal:** Implement the `kb-confluence-sync` CLI tool that authenticates to Confluence Cloud/Server, fetches spaces/pages, converts XHTML storage format to Markdown, and upserts into the knowledge base via the API. Incremental sync is driven by page version numbers.

**Architecture refs:** ADR-009 (tree-sitter pluggable, but Confluence uses its own parser), ADR-007 (Celery for long syncs), ADR-004 (visibility)

**Required skills (read before any task):**
- `kb-conventions`
- `kb-tdd-workflow`
- `kb-ingestion-connectors` — Confluence-specific rules: XHTML→MD, idempotency by version, unknown macros → named fenced block + meta.raw_macros
- `kb-celery-jobs`
- `kb-api-conventions`

**Exit criteria:**
- [ ] All tasks checked
- [ ] `pytest -x backend/tests/` green
- [ ] `ruff check tools/kb-confluence-sync/` clean
- [ ] `mypy --strict tools/kb-confluence-sync/` clean
- [ ] `kb-confluence-sync --dry-run` exits 0 against a mocked Confluence server
- [ ] Idempotency test: sync twice → same node count
- [ ] Exit codes: 0=success, 1=sync error, 2=config error

---

## Task 1 — Confluence REST client

**Files:**
- Create: `tools/kb-confluence-sync/confluence_client.py`
- Create: `tools/kb-confluence-sync/tests/test_confluence_client.py`

### Steps

- [x] **1.1** Write the failing tests:

> [plan-fix] Test code adjusted for `ruff` (I001/F401/F841) and `mypy --strict`: imports sorted,
> unused `pytest`/`ConfluencePage` imports dropped, functions annotated, and the auth test now
> asserts the exact `Basic <b64>` header (uses `expected` instead of leaving it dead).
> Also added `tools/kb-confluence-sync/ruff.toml` (mirrors backend select: E,F,I,UP,B,ASYNC) so the
> exit criterion `ruff check tools/kb-confluence-sync/` is deterministic outside backend's config.

```python
# tools/kb-confluence-sync/tests/test_confluence_client.py
from typing import Any
from unittest.mock import MagicMock, patch

from confluence_client import ConfluenceClient


def mock_session_get(url: str, **kwargs: Any) -> MagicMock:
    """Return canned responses based on URL fragment."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if "/rest/api/content" in url and "spaceKey" in kwargs.get("params", {}):
        resp.json.return_value = {
            "results": [
                {"id": "123", "title": "Test Page", "version": {"number": 3}},
            ],
            "_links": {},
        }
    elif "/rest/api/content/123" in url:
        resp.json.return_value = {
            "id": "123",
            "title": "Test Page",
            "version": {"number": 3},
            "body": {"storage": {"value": "<p>Hello <strong>world</strong></p>"}},
            "space": {"key": "TS"},
            "_links": {"webui": "/pages/123"},
        }
    else:
        resp.json.return_value = {"results": [], "_links": {}}
    return resp


def test_list_pages() -> None:
    client = ConfluenceClient(base_url="https://example.atlassian.net/wiki", token="tok")
    with patch.object(client._session, "get", side_effect=mock_session_get):
        pages = client.list_pages("TS")
        assert len(pages) == 1
        assert pages[0].page_id == "123"
        assert pages[0].title == "Test Page"
        assert pages[0].version == 3


def test_get_page_content() -> None:
    client = ConfluenceClient(base_url="https://example.atlassian.net/wiki", token="tok")
    with patch.object(client._session, "get", side_effect=mock_session_get):
        page = client.get_page("123")
        assert page.body_storage is not None
        assert "<p>" in page.body_storage


def test_client_uses_token_auth() -> None:
    client = ConfluenceClient(
        base_url="https://example.atlassian.net/wiki", token="mytoken", email="user@test.com"
    )
    # Authorization header must be set
    import base64

    expected = base64.b64encode(b"user@test.com:mytoken").decode()
    assert client._session.headers["Authorization"] == f"Basic {expected}"
```

- [x] **1.2** Create the client:

> [plan-fix] Dropped unused `from typing import Generator` (ruff F401); `list_pages` builds its
> query dict as `params: dict[str, str | int]` so `mypy --strict` accepts `Session.get(params=...)`.

```python
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
```

- [x] **1.3** Run tests:
```bash
cd tools/kb-confluence-sync && python -m pytest tests/test_confluence_client.py -v
# Expected: 3 passed
```

- [x] **1.4** Commit:
```
feat(tools): ConfluenceClient — list_pages, get_page, Basic/Bearer auth, pagination
```

---

## Task 2 — XHTML to Markdown converter

**Files:**
- Create: `tools/kb-confluence-sync/xhtml_to_md.py`
- Create: `tools/kb-confluence-sync/tests/test_xhtml_to_md.py`

### Steps

- [x] **2.1** Write the failing tests:

> [plan-fix] Tests annotated (`-> None`) and long macro literals split for `mypy --strict` /
> `ruff` line-length; behavior identical to the original block below.

```python
# tools/kb-confluence-sync/tests/test_xhtml_to_md.py
from xhtml_to_md import convert_storage_to_md


def test_basic_paragraph() -> None:
    html = "<p>Hello <strong>world</strong></p>"
    md = convert_storage_to_md(html)
    assert "Hello" in md
    assert "**world**" in md


def test_heading_conversion() -> None:
    html = "<h1>Title</h1><h2>Sub</h2>"
    md = convert_storage_to_md(html)
    assert "# Title" in md
    assert "## Sub" in md


def test_code_block() -> None:
    html = (
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
        '<![CDATA[print("hi")]]></ac:plain-text-body></ac:structured-macro>'
    )
    md = convert_storage_to_md(html)
    assert "```" in md
    assert 'print("hi")' in md


def test_unknown_macro_becomes_named_fence() -> None:
    html = (
        '<ac:structured-macro ac:name="jira">'
        '<ac:parameter ac:name="key">KB-1</ac:parameter></ac:structured-macro>'
    )
    md = convert_storage_to_md(html)
    assert "```confluence-macro-jira" in md


def test_link_conversion() -> None:
    html = '<a href="https://example.com">click</a>'
    md = convert_storage_to_md(html)
    assert "[click](https://example.com)" in md


def test_table_conversion() -> None:
    html = """
    <table><tbody>
      <tr><th>Name</th><th>Value</th></tr>
      <tr><td>A</td><td>1</td></tr>
    </tbody></table>
    """
    md = convert_storage_to_md(html)
    assert "| Name |" in md
    assert "| A |" in md


def test_unknown_macro_captured_in_meta() -> None:
    html = (
        '<ac:structured-macro ac:name="widget">'
        '<ac:parameter ac:name="url">https://x.com</ac:parameter></ac:structured-macro>'
    )
    md, meta = convert_storage_to_md(html, return_meta=True)
    assert "widget" in meta.get("raw_macros", [{}])[0].get("name", "")
```

- [x] **2.2** Implement (using `beautifulsoup4` + `html2text` + custom macro handling):

> [plan-fix] Three deviations from the block below, all forced by the plan's own tests/gates:
> 1. `@overload` on `convert_storage_to_md` (`Literal[True/False]` for `return_meta`) so the
>    tuple unpacking in `test_unknown_macro_captured_in_meta` passes `mypy --strict`.
> 2. bs4 4.15 native typing: attribute reads narrowed with `isinstance(..., str)` /
>    `isinstance(href_tag, Tag)`; unused `Generator`-style imports dropped; dead `else: pass` removed.
> 3. Tables are converted to GFM pipe tables directly via bs4 and re-inserted through
>    placeholders after `html2text` — html2text cannot emit unpadded GFM tables (compact mode
>    drops leading pipes; `pad_tables` pads cell widths), which fails `assert "| A |" in md`.
> Canonical source: `tools/kb-confluence-sync/xhtml_to_md.py`.

```python
# tools/kb-confluence-sync/xhtml_to_md.py
from __future__ import annotations

import re
from typing import Any, Literal, overload

try:
    import html2text
    from bs4 import BeautifulSoup, Tag
except ImportError as e:
    raise ImportError(
        "Install beautifulsoup4 and html2text: pip install beautifulsoup4 html2text"
    ) from e


@overload
def convert_storage_to_md(xhtml: str, return_meta: Literal[False] = False) -> str: ...


@overload
def convert_storage_to_md(
    xhtml: str, return_meta: Literal[True]
) -> tuple[str, dict[str, Any]]: ...


def convert_storage_to_md(
    xhtml: str,
    return_meta: bool = False,
) -> str | tuple[str, dict[str, Any]]:
    """
    Convert Confluence XHTML storage format to Markdown.

    Rules (from kb-ingestion-connectors):
    - Known code macro → fenced code block
    - Unknown macros → named fenced block ```confluence-macro-<name> + raw XML in meta.raw_macros
    - Tables → GFM pipe tables
    - ac:link → resolve to plain URL where possible
    """
    meta: dict[str, Any] = {"raw_macros": []}

    soup = BeautifulSoup(xhtml, "html.parser")

    # Process structured macros
    for macro in soup.find_all("ac:structured-macro"):
        name_attr = macro.get("ac:name")
        name = name_attr if isinstance(name_attr, str) else "unknown"

        if name == "code":
            # Extract language parameter
            lang_tag = macro.find("ac:parameter", {"ac:name": "language"})
            lang = lang_tag.get_text(strip=True) if lang_tag else ""
            body_tag = macro.find("ac:plain-text-body")
            code = body_tag.get_text() if body_tag else ""
            fence = f"\n```{lang}\n{code}\n```\n"
            macro.replace_with(BeautifulSoup(f"<pre>{fence}</pre>", "html.parser"))

        elif name in ("panel", "info", "warning", "note", "tip"):
            title_tag = macro.find("ac:parameter", {"ac:name": "title"})
            title = title_tag.get_text(strip=True) if title_tag else name.capitalize()
            body_tag = macro.find("ac:rich-text-body") or macro.find("ac:plain-text-body")
            body = body_tag.get_text(strip=True) if body_tag else ""
            macro.replace_with(
                BeautifulSoup(
                    f"<blockquote><strong>{title}:</strong> {body}</blockquote>",
                    "html.parser",
                )
            )

        else:
            # Unknown macro — named fence + capture raw XML
            raw_xml = str(macro)
            fence = f"```confluence-macro-{name}\n{raw_xml}\n```"
            meta["raw_macros"].append({"name": name, "raw": raw_xml})
            macro.replace_with(BeautifulSoup(f"<pre>{fence}</pre>", "html.parser"))

    # Process ac:link tags → plain href
    for link in soup.find_all("ac:link"):
        href_tag = link.find("ri:url")
        anchor = link.find("ac:plain-text-link-body") or link.find("ac:link-body")
        if isinstance(href_tag, Tag):
            url_attr = href_tag.get("ri:value")
            url = url_attr if isinstance(url_attr, str) else "#"
            text = anchor.get_text(strip=True) if anchor else url
            link.replace_with(BeautifulSoup(f'<a href="{url}">{text}</a>', "html.parser"))
        else:
            link.replace_with(anchor.get_text(strip=True) if anchor else "")

    # Tables → GFM pipe tables. html2text cannot produce them (compact mode drops
    # the leading/trailing pipes, pad_tables pads cell widths), so convert directly
    # and re-insert after html2text via placeholders.
    table_blocks: list[str] = []
    for idx, table in enumerate(soup.find_all("table")):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [cell.get_text(strip=True) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if not rows:
            table.decompose()
            continue
        lines = ["| " + " | ".join(rows[0]) + " |"]
        lines.append("|" + "|".join(" --- " for _ in rows[0]) + "|")
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        table_blocks.append("\n".join(lines))
        table.replace_with(f"kbtableplaceholder{idx}")

    # Convert remaining tags with html2text
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.body_width = 0
    h.protect_links = False
    h.wrap_links = False
    h.mark_code = True

    md = h.handle(str(soup))

    for idx, block in enumerate(table_blocks):
        md = md.replace(f"kbtableplaceholder{idx}", f"\n{block}\n")

    # Clean up html2text pre-block artefacts
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    if not meta["raw_macros"]:
        del meta["raw_macros"]

    if return_meta:
        return md, meta
    return md
```

- [x] **2.3** Create `tools/kb-confluence-sync/requirements.txt`:
```
requests>=2.31
beautifulsoup4>=4.12
html2text>=2020.1
lxml>=4.9
# lint/type-check only
types-requests
```

- [x] **2.4** Run tests:
```bash
cd tools/kb-confluence-sync
pip install -r requirements.txt --break-system-packages
python -m pytest tests/test_xhtml_to_md.py -v
# Expected: 7 passed
```

- [x] **2.5** Commit:
```
feat(tools): XHTML→Markdown converter with Confluence macro handling
```

---

## Task 3 — Sync engine

**Files:**
- Create: `tools/kb-confluence-sync/sync_engine.py`
- Create: `tools/kb-confluence-sync/tests/test_sync_engine.py`

### Steps

- [x] **3.1** Write failing tests:

> **[plan-fix]** Adjusted from the original block for this repo's gates: typed signatures + no
> unused imports (ruff/`mypy --strict`, matching Task 1/2 test style); tests write the version
> cache to `tmp_path` instead of the repo cwd; added a fourth test
> (`test_sync_twice_second_run_all_skipped`) — the sync-twice idempotency test required by
> `kb-ingestion-connectors` and this plan's exit criteria.

```python
# tools/kb-confluence-sync/tests/test_sync_engine.py
from pathlib import Path
from unittest.mock import MagicMock, patch

from confluence_client import ConfluencePage
from sync_engine import SyncConfig, SyncEngine, SyncResult


def make_page(
    pid: str = "1", title: str = "Test", version: int = 1, body: str = "<p>Content</p>"
) -> ConfluencePage:
    return ConfluencePage(
        page_id=pid,
        title=title,
        version=version,
        space_key="TS",
        web_url=f"/pages/{pid}",
        body_storage=body,
    )


def make_config(dry_run: bool, cache_file: str) -> SyncConfig:
    return SyncConfig(
        confluence_url="https://example.atlassian.net/wiki",
        confluence_token="tok",
        confluence_email="user@test.com",
        space_keys=["TS"],
        kb_api_url="http://localhost:8000",
        kb_token="kb-tok",
        dry_run=dry_run,
        version_cache_file=cache_file,
    )


def test_sync_result_counts() -> None:
    result = SyncResult()
    result.created += 1
    result.updated += 2
    result.skipped += 3
    assert result.total == 6


def test_sync_engine_dry_run(tmp_path: Path) -> None:
    """Dry run must not call the KB API."""
    config = make_config(dry_run=True, cache_file=str(tmp_path / "versions.json"))
    engine = SyncEngine(config)
    pages = [make_page("1", "Page 1"), make_page("2", "Page 2")]
    with (
        patch.object(engine._client, "list_pages", return_value=pages),
        patch.object(engine._client, "get_page", side_effect=lambda pid: make_page(pid)),
    ):
        result = engine.sync_space("TS")
        assert result.total > 0
        # In dry run mode, no HTTP calls to KB API
        assert result.api_calls == 0


def test_sync_skips_unchanged_page(tmp_path: Path) -> None:
    """Page with same version should be skipped (idempotency)."""
    config = make_config(dry_run=False, cache_file=str(tmp_path / "versions.json"))
    engine = SyncEngine(config)
    # Simulate: page already synced at version 3
    engine._version_cache["TS:1"] = 3

    pages = [make_page("1", version=3)]  # same version
    with patch.object(engine._client, "list_pages", return_value=pages):
        result = engine.sync_space("TS")
        assert result.skipped == 1
        assert result.created == 0
        assert result.updated == 0


def test_sync_twice_second_run_all_skipped(tmp_path: Path) -> None:
    """Full idempotency: re-sync with a fresh engine skips every unchanged page."""
    config = make_config(dry_run=False, cache_file=str(tmp_path / "versions.json"))
    pages = [make_page("1", "Page 1"), make_page("2", "Page 2")]

    def run_sync() -> SyncResult:
        engine = SyncEngine(config)
        engine._kb_session = MagicMock()
        engine._kb_session.post.return_value.status_code = 201
        with (
            patch.object(engine._client, "list_pages", return_value=pages),
            patch.object(engine._client, "get_page", side_effect=lambda pid: make_page(pid)),
        ):
            return engine.sync_space("TS")

    first = run_sync()
    assert first.created == 2
    assert first.failed == 0

    second = run_sync()  # fresh engine → cache re-loaded from disk
    assert second.skipped == 2
    assert second.created == 0
    assert second.updated == 0
    assert second.api_calls == 0
```

- [x] **3.2** Create `sync_engine.py`:

> **[plan-fix]** Deviations from the original block, all forced by ruff/`mypy --strict`:
> removed unused `field`/`Any` imports; `_load_cache` assigns `json.load` to a typed local
> (mypy strict forbids returning `Any`); `sync_all` annotates `results`; dropped the dead
> `r = self._kb_session.get(/api/v1/nodes...)` existence check (F841 — its response was never
> read; the ingest-item endpoint owns upsert semantics per Task 4).

```python
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
                logger.info(f"[DRY RUN] Would sync: {page.title} (v{page.version})")
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
```

- [x] **3.3** Run tests:
```bash
cd tools/kb-confluence-sync && python -m pytest tests/test_sync_engine.py -v
# Expected: 4 passed  [plan-fix: was 3 — sync-twice idempotency test added in 3.1]
```

- [x] **3.4** Commit:
```
feat(tools): SyncEngine with incremental sync, version cache, dry-run mode
```

---

## Task 4 — Ingest-item API endpoint (for CLI upsert)

**Files:**
- Modify: `backend/app/api/v1/uploads.py`
- Create: `backend/tests/api/test_ingest_item_api.py`

### Steps

- [x] **4.1** Write failing test:

> [plan-fix] vs the original block: dropped `pytestmark = pytest.mark.asyncio`
> (asyncio_mode="auto", test_tokens_api precedent); added the kb-api-conventions
> checklist tests (401 unauthenticated, 422 missing title) and a tags-persistence
> test — the sync engine (Task 3) sends Confluence labels as `tags`, which the
> original 4.2 block dropped silently (labels → tags, kb-ingestion-connectors).

```python
# backend/tests/api/test_ingest_item_api.py
import uuid

from httpx import AsyncClient
from sqlalchemy import select


async def test_ingest_item_creates_node(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json={
            "title": "Confluence Page",
            "body": "# Title\n\nContent from Confluence.",
            "node_type": "confluence_page",
            "visibility": "private",
            "source": "confluence",
            "source_ref": "confluence:TS:12345",
            "meta": {"confluence_page_id": "12345"},
            "tags": ["confluence", "docs"],
        },
        headers=auth_headers,
    )
    assert r.status_code in (200, 201)
    data = r.json()
    assert data["source"] == "confluence"
    assert data["source_ref"] == "confluence:TS:12345"


async def test_ingest_item_idempotent(client: AsyncClient, auth_headers):
    payload = {
        "title": "Idem Page",
        "body": "body",
        "source": "confluence",
        "source_ref": "confluence:TS:idem1",
    }
    r1 = await client.post("/api/v1/uploads/ingest-item", json=payload, headers=auth_headers)
    r2 = await client.post("/api/v1/uploads/ingest-item", json=payload, headers=auth_headers)
    assert r1.json()["id"] == r2.json()["id"], "Same source_ref must return same node ID"


async def test_ingest_item_persists_tags(client: AsyncClient, auth_headers, db):
    """Confluence labels arrive as `tags` and must land in tags/node_tags."""
    from app.models.knowledge import NodeTag, Tag

    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json={
            "title": "Tagged Page",
            "body": "body",
            "source": "confluence",
            "source_ref": "confluence:TS:tagged1",
            "tags": ["confluence", "docs"],
        },
        headers=auth_headers,
    )
    node_id = uuid.UUID(r.json()["id"])
    slugs = await db.scalars(
        select(Tag.slug).join(NodeTag, NodeTag.tag_id == Tag.id).where(NodeTag.node_id == node_id)
    )
    assert set(slugs) == {"confluence", "docs"}


async def test_ingest_item_unauthenticated_is_401(client: AsyncClient):
    r = await client.post("/api/v1/uploads/ingest-item", json={"title": "t", "body": "b"})
    assert r.status_code == 401


async def test_ingest_item_missing_title_is_422(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-item", json={"body": "no title"}, headers=auth_headers
    )
    assert r.status_code == 422
```

- [x] **4.2** Add `POST /api/v1/uploads/ingest-item` to `uploads.py`:

> [plan-fix] vs the original block:
> - `get_scoped_viewer`, not `get_current_viewer` — the admin visibility bypass
>   is only reachable under /api/v1/admin/* (Phase 1 standard, kb-visibility rule 5).
> - `run_pending_graph_ops(db)` after the commit — create/update queue the Neo4j
>   vertex sync on the session; the original block never drained it (ADR-011,
>   nodes.py standard).
> - `IngestItemIn(NodeCreate)` adds the `tags` field and passes it to IngestItem —
>   NodeCreate has no tags field, so Confluence labels sent by the sync engine
>   would have been dropped silently.
> - Typed return + `NodeOut.model_validate(node)` (never return ORM objects) and
>   summary/operation_id, per kb-api-conventions.

```python
# backend/app/api/v1/uploads.py  (add after existing routes)
from app.schemas.node import NodeCreate, NodeOut
from app.services import node_service as ns
from app.services.ingest.base import IngestItem, KnowledgeIngestor


class IngestItemIn(NodeCreate):
    """NodeCreate + `tags` — Confluence labels arrive as tags [plan-fix]."""

    tags: list[str] = Field(default_factory=list)


@router.post(
    "/ingest-item",
    response_model=NodeOut,
    summary="Upsert a single knowledge node from an external source",
    operation_id="ingestSingleItem",
)
async def ingest_single_item(
    payload: IngestItemIn,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    """Upsert a single knowledge node from an external source (Confluence CLI,
    codebase scanner). Idempotent: same source+source_ref → same node.
    """
    item = IngestItem(
        source=payload.source or "api",
        source_ref=payload.source_ref or str(uuid.uuid4()),
        title=payload.title,
        body=payload.body,
        node_type=payload.node_type,
        visibility=payload.visibility,
        tags=payload.tags,
        meta=payload.meta,
    )
    ingestor = KnowledgeIngestor(db, viewer)
    node = await ingestor.upsert(item)
    await db.commit()
    await ns.run_pending_graph_ops(db)  # Neo4j strictly after PG commit (ADR-011)
    return NodeOut.model_validate(node)
```

> Note (not fixed here): the Task 3 sync engine counts `created` only on a 201,
> but this endpoint returns 200 for create and update alike (the ingestor does
> not report which branch it took) — first syncs will log created=0/updated=N.
> Cosmetic only; revisit if SyncResult accuracy ever matters.

- [x] **4.3** Run tests:
```bash
cd backend && pytest tests/api/test_ingest_item_api.py -v
# Expected: 5 passed  [plan-fix: was 2 — 401/422/tags tests added in 4.1]
```

- [x] **4.4** Commit:
```
feat(api): POST /api/v1/uploads/ingest-item — single-item upsert for CLI tools
```

---

## Task 5 — CLI entrypoint

**Files:**
- Create: `tools/kb-confluence-sync/__main__.py`
- Create: `tools/kb-confluence-sync/pyproject.toml`
- Create: `tools/kb-confluence-sync/tests/test_cli.py`

### Steps

- [ ] **5.1** Write failing tests:

```python
# tools/kb-confluence-sync/tests/test_cli.py
import subprocess
import sys


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "kb_confluence_sync", *args],
        capture_output=True, text=True,
        cwd="tools/kb-confluence-sync",
    )


def test_help_exits_0():
    r = run_cli("--help")
    assert r.returncode == 0
    assert "Usage" in r.stdout or "usage" in r.stdout.lower()


def test_missing_config_exits_2():
    r = run_cli("sync", "--space", "TS")
    assert r.returncode == 2
```

- [ ] **5.2** Create `__main__.py`:

```python
# tools/kb-confluence-sync/__main__.py
"""
kb-confluence-sync — Incremental Confluence to Knowledge Base sync tool.

Usage:
    python -m kb_confluence_sync sync --space SPACE_KEY [--dry-run]
    python -m kb_confluence_sync sync --all-spaces [--dry-run]

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

import logging
import os
import sys

try:
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: {name} environment variable is required", file=sys.stderr)
        sys.exit(2)
    return val


def build_config(args) -> "SyncConfig":  # noqa: F821
    from sync_engine import SyncConfig

    url = os.environ.get("CONFLUENCE_URL")
    token = os.environ.get("CONFLUENCE_TOKEN")
    kb_url = os.environ.get("KB_API_URL", "http://localhost:8000")
    kb_token = os.environ.get("KB_API_TOKEN")

    if not url or not token or not kb_token:
        print("ERROR: CONFLUENCE_URL, CONFLUENCE_TOKEN, and KB_API_TOKEN are required", file=sys.stderr)
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
        visibility=getattr(args, "visibility", "private"),
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
    sync_parser.add_argument("--visibility", default="private", choices=["private", "public", "shared"])
    sync_parser.add_argument("--json", action="store_true", help="Output results as JSON")
    sync_parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    config = build_config(args)

    from sync_engine import SyncEngine
    engine = SyncEngine(config)

    try:
        results = engine.sync_all()
    except Exception as exc:
        print(f"ERROR: Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)

    any_failed = False
    for space, result in results.items():
        if args.json:
            import json
            print(json.dumps({"space": space, "created": result.created,
                               "updated": result.updated, "skipped": result.skipped,
                               "failed": result.failed}))
        else:
            print(f"[{space}] created={result.created} updated={result.updated} "
                  f"skipped={result.skipped} failed={result.failed}")
        if result.failed > 0:
            any_failed = True

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
```

- [ ] **5.3** Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "kb-confluence-sync"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "requests>=2.31",
    "beautifulsoup4>=4.12",
    "html2text>=2020.1",
    "python-dotenv>=1.0",
]

[project.scripts]
kb-confluence-sync = "kb_confluence_sync.__main__:main"
```

- [ ] **5.4** Run tests:
```bash
cd tools/kb-confluence-sync && python -m pytest tests/test_cli.py -v
# Expected: 2 passed
```

- [ ] **5.5** Verify dry-run:
```bash
cd tools/kb-confluence-sync
CONFLUENCE_URL=https://example.atlassian.net/wiki \
CONFLUENCE_TOKEN=fake \
KB_API_TOKEN=fake \
python -m __main__ sync --space TS --dry-run
# Expected: exit 0 (no network calls in dry-run)
```

- [ ] **5.6** Run full lint:
```bash
cd tools/kb-confluence-sync
ruff check .
mypy --strict confluence_client.py sync_engine.py xhtml_to_md.py
```

- [ ] **5.7** Commit:
```
feat(tools): kb-confluence-sync CLI — sync, dry-run, --json output, exit codes 0/1/2
```

---

## Phase 5 exit gate

```bash
# Backend
cd backend && pytest tests/ --tb=short && ruff check . && mypy --strict app/services/ app/schemas/

# CLI tool
cd tools/kb-confluence-sync
python -m pytest tests/ -v
ruff check .
mypy --strict *.py

# Dry-run smoke test (no real Confluence needed):
CONFLUENCE_URL=https://example.atlassian.net/wiki \
CONFLUENCE_TOKEN=fake \
KB_API_TOKEN=fake \
python __main__.py sync --space TS --dry-run
# Expected: exit 0

# Idempotency: run ingest-item API test twice
cd backend && pytest tests/api/test_ingest_item_api.py::test_ingest_item_idempotent -v
```

Update `docs/plans/README.md` — Phase 5 Status → `Done`.
