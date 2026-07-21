// frontend/tests/unit/api.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest"
import { fetchNodes, fetchNode, searchNodes } from "@/lib/api"

// Mock fetch globally
const mockFetch = vi.fn()
global.fetch = mockFetch

beforeEach(() => { mockFetch.mockReset() })

describe("fetchNodes", () => {
  it("calls /api/v1/nodes and returns data", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [], total: 0, offset: 0, limit: 50 }),
    })
    const result = await fetchNodes()
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/nodes"),
      expect.objectContaining({ credentials: "include" })
    )
    expect(result.total).toBe(0)
  })
})

describe("searchNodes", () => {
  it("encodes query parameter", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ items: [], total: 0, query: "hello world" }),
    })
    await searchNodes("hello world")
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("q=hello+world"),
      expect.any(Object)
    )
  })
})
