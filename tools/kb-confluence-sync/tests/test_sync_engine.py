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
