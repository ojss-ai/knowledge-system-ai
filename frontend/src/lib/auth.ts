// frontend/src/lib/auth.ts
export interface TokenClaims {
  sub: string
  role: string
  exp: number
  iat: number
}

export function parseAccessToken(token: string): TokenClaims | null {
  if (!token) return null
  try {
    const parts = token.split(".")
    if (parts.length !== 3) return null
    const payload = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")))
    return payload as TokenClaims
  } catch {
    return null
  }
}

export function isTokenExpired(claims: TokenClaims): boolean {
  return Date.now() / 1000 > claims.exp
}
