from fastapi import FastAPI

from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="Knowledge Base API", version="0.1.0")
    register_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
