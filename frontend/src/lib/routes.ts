// frontend/src/lib/routes.ts
// Pure routing predicate used by middleware.ts — kept here so vitest can
// unit-test it without a Next.js edge runtime.

const PUBLIC_PAGE_PREFIXES = ["/login"]

/**
 * Should an unauthenticated request to this pathname be redirected to /login?
 * Only page navigations redirect. API routes (/api/*) are always excluded:
 * they must receive a JSON 401 from the BFF proxy / backend, never a 307
 * HTML redirect.
 */
export function requiresLoginRedirect(pathname: string): boolean {
  if (pathname === "/api" || pathname.startsWith("/api/")) return false
  return !PUBLIC_PAGE_PREFIXES.some((p) => pathname.startsWith(p))
}
