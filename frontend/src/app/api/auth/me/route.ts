// frontend/src/app/api/auth/me/route.ts
import { NextRequest, NextResponse } from "next/server"

export async function GET(req: NextRequest) {
  const token = req.cookies.get("access_token")?.value
  if (!token) return NextResponse.json({ error: "Not authenticated" }, { status: 401 })

  const apiRes = await fetch(
    `${process.env.API_BASE_URL ?? "http://localhost:8000"}/api/v1/users/me`,
    { headers: { Authorization: `Bearer ${token}` } }
  )
  if (!apiRes.ok) return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  return NextResponse.json(await apiRes.json())
}
