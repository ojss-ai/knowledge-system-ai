// frontend/tests/unit/routes.test.ts
// Middleware login-redirect scope: only page navigations redirect to /login;
// /api/* must never receive a 307 HTML redirect (JSON 401 comes from the
// BFF proxy / backend instead).
import { describe, it, expect } from "vitest"
import { NextRequest } from "next/server"
import { requiresLoginRedirect } from "@/lib/routes"
import { middleware } from "@/middleware"

describe("requiresLoginRedirect", () => {
  it("protects page navigations", () => {
    expect(requiresLoginRedirect("/")).toBe(true)
    expect(requiresLoginRedirect("/graph")).toBe(true)
    expect(requiresLoginRedirect("/nodes/abc")).toBe(true)
  })

  it("excludes the login page", () => {
    expect(requiresLoginRedirect("/login")).toBe(false)
  })

  it("excludes all API routes", () => {
    expect(requiresLoginRedirect("/api")).toBe(false)
    expect(requiresLoginRedirect("/api/v1/nodes")).toBe(false)
    expect(requiresLoginRedirect("/api/auth/login")).toBe(false)
  })
})

describe("middleware", () => {
  it("redirects unauthenticated page navigation to /login", () => {
    const res = middleware(new NextRequest("http://localhost:3000/graph"))
    expect(res.status).toBe(307)
    expect(res.headers.get("location")).toBe("http://localhost:3000/login")
  })

  it("never redirects unauthenticated API calls", () => {
    const res = middleware(new NextRequest("http://localhost:3000/api/v1/nodes"))
    expect(res.status).toBe(200)
    expect(res.headers.get("location")).toBeNull()
  })
})
