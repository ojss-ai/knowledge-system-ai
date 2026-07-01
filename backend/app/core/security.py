import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings

ALGO = "HS256"


def _make(sub: str, role: str, ttl: int, kind: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": sub,
            "role": role,
            "kind": kind,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(seconds=ttl),
        },
        settings.jwt_secret,
        algorithm=ALGO,
    )


def make_access_token(user_id: uuid.UUID, role: str) -> str:
    return _make(str(user_id), role, settings.jwt_access_ttl_seconds, "access")


def make_refresh_token(user_id: uuid.UUID, role: str) -> str:
    return _make(str(user_id), role, settings.jwt_refresh_ttl_seconds, "refresh")


def decode_token(token: str, expected_kind: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
    if payload.get("kind") != expected_kind:
        raise jwt.InvalidTokenError("wrong token kind")
    return payload
