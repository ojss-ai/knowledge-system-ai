// frontend/tests/unit/proxy.test.ts
// BFF proxy: /api/v1/[...path] route handler must attach Authorization from the
// httpOnly access_token cookie and pass through status/body untouched.
import { describe, it, expect, vi, beforeEach } from "vitest"
import { NextRequest } from "next/server"
import { GET, POST, DELETE } from "@/app/api/v1/[...path]/route"

const mockFetch = vi.fn()
global.fetch = mockFetch

beforeEach(() => {
  mockFetch.mockReset()
})

function makeRequest(
  url: string,
  init?: { method?: string; body?: string; headers?: HeadersInit; cookie?: string }
) {
  const headers = new Headers(init?.headers)
  if (init?.cookie) headers.set("cookie", init.cookie)
  return new NextRequest(url, { method: init?.method, body: init?.body, headers })
}

describe("BFF /api/v1 proxy", () => {
  it("attaches Authorization: Bearer from the access_token cookie", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    )
    const req = makeRequest("http://localhost:3000/api/v1/nodes", {
      cookie: "access_token=tok-123",
    })
    await GET(req, { params: { path: ["nodes"] } })

    expect(mockFetch).toHaveBeenCalledTimes(1)
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe("http://localhost:8000/api/v1/nodes")
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer tok-123")
  })

  it("preserves the query string when forwarding", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    )
    const req = makeRequest(
      "http://localhost:3000/api/v1/search?q=hello+world&limit=20",
      { cookie: "access_token=tok-123" }
    )
    await GET(req, { params: { path: ["search"] } })

    const [url] = mockFetch.mock.calls[0]
    expect(url).toBe("http://localhost:8000/api/v1/search?q=hello+world&limit=20")
  })

  it("forwards without Authorization when no cookie (backend 401s cleanly)", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Not authenticated" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      })
    )
    const req = makeRequest("http://localhost:3000/api/v1/nodes")
    const res = await GET(req, { params: { path: ["nodes"] } })

    const [, init] = mockFetch.mock.calls[0]
    expect(new Headers(init.headers).get("authorization")).toBeNull()
    expect(res.status).toBe(401)
  })

  it("passes 204 No Content through with an empty body", async () => {
    mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }))
    const req = makeRequest("http://localhost:3000/api/v1/nodes/abc", {
      cookie: "access_token=tok-123",
    })
    const res = await DELETE(req, { params: { path: ["nodes", "abc"] } })

    expect(res.status).toBe(204)
    expect(await res.text()).toBe("")
  })

  it("forwards the request body and passes through backend status + JSON", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ id: "n1", title: "New" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      })
    )
    const req = makeRequest("http://localhost:3000/api/v1/nodes", {
      method: "POST",
      cookie: "access_token=tok-123",
      body: JSON.stringify({ title: "New" }),
      headers: { "content-type": "application/json" },
    })
    const res = await POST(req, { params: { path: ["nodes"] } })

    const [, init] = mockFetch.mock.calls[0]
    expect(init.method).toBe("POST")
    expect(init.body).toBe(JSON.stringify({ title: "New" }))
    expect(res.status).toBe(201)
    expect(await res.json()).toEqual({ id: "n1", title: "New" })
  })
})
