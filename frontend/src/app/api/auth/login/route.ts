// frontend/src/app/api/auth/login/route.ts
import { NextRequest, NextResponse } from "next/server"

export async function POST(req: NextRequest) {
  const { email, password } = await req.json()
  // [plan-fix] backend login is JSON {email, password} (LoginIn schema in
  // backend/app/schemas/auth.py), not OAuth2 form-urlencoded {username, password}
  const apiRes = await fetch(
    `${process.env.API_BASE_URL ?? "http://localhost:8000"}/api/v1/auth/login`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }
  )
  if (!apiRes.ok) {
    return NextResponse.json({ error: "Invalid credentials" }, { status: 401 })
  }
  const tokens = await apiRes.json()
  const res = NextResponse.json({ ok: true })
  // Store tokens in httpOnly cookies — never exposed to JS
  res.cookies.set("access_token", tokens.access_token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: 900,
    path: "/",
  })
  res.cookies.set("refresh_token", tokens.refresh_token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: 604800,
    path: "/api/auth/refresh",
  })
  return res
}
