# ADR-008: JWT auth, httpOnly cookies, optional OIDC

**Status:** Accepted · 2026-06-12

## Context
Self-hosted deployment; company may later want SSO (Keycloak/Azure AD). CLI tools need non-interactive auth.

## Decision
Email/password (argon2id) issuing short-lived access JWT (15 min) + rotating refresh token (7 d). Browser receives tokens only as httpOnly Secure cookies via Next.js BFF route handlers — never localStorage. CLI/service accounts use long-lived scoped API tokens (hashed at rest, revocable). OIDC is a pluggable login provider added behind the same session issuance, not a parallel auth path.

## Consequences
- Stateless API replicas; logout = refresh-token revocation list in Redis.
- BFF pattern means the frontend never handles raw tokens (XSS-resistant).
- Service tokens carry `role=service` and per-connector scopes; audited like users.

## Revisit when
Company mandates SSO-only, or token revocation latency requirements tighten.
