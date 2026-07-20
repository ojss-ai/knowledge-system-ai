import pytest
from neo4j import AsyncSession as Neo4jSession

from app.core.config import settings
from app.main import create_app

pytestmark = pytest.mark.asyncio


async def test_neo4j_reachable(neo4j_session: Neo4jSession):
    """Driver can execute a trivial query against the running Neo4j instance."""
    result = await neo4j_session.run("RETURN 1 AS ok")
    record = await result.single()
    assert record["ok"] == 1


async def test_node_id_constraint_exists(neo4j_session: Neo4jSession):
    """Uniqueness constraint on :Node(node_id) must exist."""
    result = await neo4j_session.run(
        "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties "
        "WHERE labelsOrTypes = ['Node'] AND properties = ['node_id'] RETURN name"
    )
    records = await result.data()
    assert len(records) >= 1, "Missing uniqueness constraint on :Node(node_id)"


async def test_app_starts_when_neo4j_down(monkeypatch):
    """Startup must not crash if Neo4j is unreachable: constraints are retried later."""
    monkeypatch.setattr(settings, "neo4j_uri", "bolt://localhost:1")  # nothing listens here
    app = create_app()
    async with app.router.lifespan_context(app):
        pass  # reaching here means startup survived Neo4j being down
