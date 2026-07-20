"""Neo4j async driver singleton — import get_driver() everywhere."""

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.core.config import settings

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def ensure_constraints() -> None:
    """Idempotent: create uniqueness constraint on :Node(node_id)."""
    async with get_driver().session() as session:
        await session.run(
            "CREATE CONSTRAINT node_id_unique IF NOT EXISTS "
            "FOR (n:Node) REQUIRE n.node_id IS UNIQUE"
        )
