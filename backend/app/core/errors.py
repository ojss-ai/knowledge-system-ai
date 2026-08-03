from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from neo4j.exceptions import ServiceUnavailable


class DomainError(Exception):
    status_code = 500


class NotFoundError(DomainError):
    status_code = 404


class ForbiddenError(DomainError):
    status_code = 403


class ConflictError(DomainError):
    status_code = 409


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"detail": str(exc) or exc.__class__.__name__}
        )

    @app.exception_handler(ServiceUnavailable)
    async def neo4j_unavailable_handler(_: Request, exc: ServiceUnavailable) -> JSONResponse:
        # Endpoints whose result REQUIRES Neo4j (edge writes, traversals) surface
        # a down graph store as 503 rather than a 500 (ADR-011: PG-backed reads
        # and post-commit best-effort syncs are unaffected — this only triggers
        # where the graph is the data source itself).
        return JSONResponse(status_code=503, content={"detail": "graph backend unavailable"})
