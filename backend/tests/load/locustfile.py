"""
Load test for the Knowledge Base API.
Run: locust -f tests/load/locustfile.py --headless -u 50 -r 10 -t 60s --host http://localhost:8000

Target: p95 latency < 500ms at 50 concurrent users.

Prereqs: running stack (docker compose up) and the seeded admin user:
    python -m app.scripts.seed_admin admin@example.com admin1234
([plan-fix] admin@kb.local fails EmailStr at the HTTP layer — email-validator
rejects the special-use `.local` domain with 422 — so the load test uses a
validatable address.)

Rate limiting note: all simulated users authenticate as the same admin account,
so /api/v1/search (60/min) and /api/v1/ask (20/min) share ONE rate bucket
(rate_limit.py keys on the JWT `sub` claim). HTTP 429 on those two endpoints is
the limiter working as designed and is recorded as a pass; any other non-2xx is
a real failure. /nodes and /graph/overview are not rate limited — the gate
requires a 0% error rate there.
"""

import random
from collections import Counter
from typing import Any

from locust import HttpUser, between, events, task

RATE_LIMITED: Counter[str] = Counter()


@events.quitting.add_listener
def _report_rate_limited(environment: Any, **kwargs: Any) -> None:
    """Print how many passes were actually 429s, so the gate stays honest."""
    for name, n in sorted(RATE_LIMITED.items()):
        print(f"[rate-limited] {name}: {n} responses were 429 (counted as pass)")


SAMPLE_QUERIES = [
    "Python",
    "FastAPI",
    "graph database",
    "knowledge base",
    "embeddings",
    "vector search",
    "Confluence",
    "daily log",
]


class KBUser(HttpUser):
    wait_time = between(0.1, 1.0)
    _token: str = ""

    def on_start(self) -> None:
        """Log in and store access token."""
        # [plan-fix] /auth/login takes JSON {email, password} (schemas/auth.py
        # LoginIn), not OAuth2 form data with a `username` field.
        r = self.client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "admin1234"},
        )
        self._token = r.json().get("access_token", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    @task(5)
    def search(self) -> None:
        q = random.choice(SAMPLE_QUERIES)
        # [plan-fix] 429 = sliding-window limiter doing its job (single shared
        # admin bucket) — mark success so failures only count real errors.
        with self.client.get(
            f"/api/v1/search?q={q}",
            headers=self._headers(),
            name="/api/v1/search",
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                RATE_LIMITED["/api/v1/search"] += 1
                resp.success()

    @task(3)
    def list_nodes(self) -> None:
        self.client.get("/api/v1/nodes?limit=20", headers=self._headers(), name="/api/v1/nodes")

    @task(1)
    def graph_overview(self) -> None:
        self.client.get(
            "/api/v1/graph/overview?limit=50",
            headers=self._headers(),
            name="/api/v1/graph/overview",
        )

    @task(1)
    def ask(self) -> None:
        q = random.choice(SAMPLE_QUERIES)
        with self.client.post(
            "/api/v1/ask",
            json={"query": q},
            headers=self._headers(),
            name="/api/v1/ask",
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                RATE_LIMITED["/api/v1/ask"] += 1
                resp.success()
