import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_rate_limit_on_ask_endpoint(client: AsyncClient, auth_headers):
    """The /ask endpoint should rate-limit after N requests per window."""
    # Send many requests rapidly
    responses = []
    for _ in range(30):
        r = await client.post("/api/v1/ask", json={"query": "test"}, headers=auth_headers)
        responses.append(r.status_code)

    # At least one should be 429
    assert 429 in responses, "Rate limiting must return 429 after burst"
