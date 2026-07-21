// frontend/src/app/api/auth/logout/route.ts
import { NextResponse } from "next/server"

export async function POST() {
  const res = NextResponse.json({ ok: true })
  res.cookies.delete("access_token")
  res.cookies.delete("refresh_token")
  return res
}
