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
