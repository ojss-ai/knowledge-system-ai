from sqlalchemy import text


async def test_db_session_executes(db) -> None:
    result = await db.execute(text("SELECT 1"))
    assert result.scalar_one() == 1


async def test_extensions_present(db) -> None:
    result = await db.execute(text("SELECT extname FROM pg_extension"))
    names = {row[0] for row in result}
    assert {"vector"} <= names
