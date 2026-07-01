from fastapi import FastAPI

from app.api.v1.auth import router as auth_router
from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="Knowledge Base API", version="0.1.0")
    register_error_handlers(app)
    app.include_router(auth_router, prefix="/api/v1")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
