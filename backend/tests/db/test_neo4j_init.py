import pytest
from neo4j import AsyncSession as Neo4jSession

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
