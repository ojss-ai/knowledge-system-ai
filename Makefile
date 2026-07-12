.PHONY: up up-full down api test lint type verify openapi

up: ; docker compose -f docker/docker-compose.yml up -d
up-full: ; docker compose -f docker/docker-compose.yml --profile full up -d --build
down: ; docker compose -f docker/docker-compose.yml --profile full down
api: ; cd backend && uvicorn app.main:app --reload --port 8000
test: ; cd backend && pytest -q
lint: ; cd backend && ruff check . && ruff format --check .
type: ; cd backend && mypy app
verify: lint type test
openapi: ; cd backend && python -m app.scripts.export_openapi && cd ../frontend && npm run codegen
