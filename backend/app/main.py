from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.core.errors import register_error_handlers
from app.core.neo4j import close_driver, ensure_constraints


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await ensure_constraints()
    yield
    await close_driver()


def create_app() -> FastAPI:
    app = FastAPI(title="Knowledge Base API", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
