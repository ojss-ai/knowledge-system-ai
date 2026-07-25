// frontend/tests/unit/api.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest"
import { fetchNodes, fetchNode, searchNodes, fetchAdminStats } from "@/lib/api"

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

describe("fetchAdminStats", () => {
  it("calls /api/v1/admin/stats with credentials and returns stats", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        total_users: 3,
        active_users: 2,
        total_nodes: 10,
        total_chunks: 42,
        total_audit_events: 7,
      }),
    })
    const stats = await fetchAdminStats()
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/v1/admin/stats",
      expect.objectContaining({ credentials: "include" })
    )
    expect(stats.total_nodes).toBe(10)
  })

  it("throws on 403 (non-admin)", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      text: async () => "admin required",
    })
    await expect(fetchAdminStats()).rejects.toThrow("API 403")
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
