// frontend/src/app/api/v1/[...path]/route.ts
// BFF proxy (ADR-008): every browser call to /api/v1/* lands here. We read the
// httpOnly access_token cookie server-side and forward to FastAPI with an
// Authorization: Bearer header — the token is never exposed to client JS.
// No cookie → forward without the header; the backend 401s cleanly (JSON).
import { NextRequest } from "next/server"

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000"

interface Ctx {
  params: { path: string[] }
}

async function proxy(req: NextRequest, { params }: Ctx): Promise<Response> {
  const url = `${API_BASE_URL}/api/v1/${params.path.join("/")}${req.nextUrl.search}`

  const headers = new Headers()
  const contentType = req.headers.get("content-type")
  if (contentType) headers.set("content-type", contentType)
  const token = req.cookies.get("access_token")?.value
  if (token) headers.set("authorization", `Bearer ${token}`)

  const init: RequestInit = { method: req.method, headers, cache: "no-store" }
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text()
  }

  const apiRes = await fetch(url, init)

  // 204/304 must not carry a body
  if (apiRes.status === 204 || apiRes.status === 304) {
    return new Response(null, { status: apiRes.status })
  }
  return new Response(await apiRes.text(), {
    status: apiRes.status,
    headers: {
      "content-type": apiRes.headers.get("content-type") ?? "application/json",
    },
  })
}

export async function GET(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx)
}

export async function POST(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx)
}

export async function PATCH(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx)
}

export async function DELETE(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx)
}
