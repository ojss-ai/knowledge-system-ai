// frontend/src/lib/api.ts
import type { KBNode, NodeListOut, SearchOut, GraphData, AdminStats } from "./types"

const BASE = "/api/v1"

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`API ${res.status}: ${text}`)
  }
  // [plan-fix] DELETE /nodes/{id} returns 204 No Content — res.json() would
  // throw on the empty body.
  if (res.status === 204) {
    return undefined as T
  }
  return res.json() as Promise<T>
}

export async function fetchNodes(offset = 0, limit = 50): Promise<NodeListOut> {
  return apiFetch(`/nodes?offset=${offset}&limit=${limit}`)
}

export async function fetchNode(id: string): Promise<KBNode> {
  return apiFetch(`/nodes/${id}`)
}

export async function createNode(data: Partial<KBNode>): Promise<KBNode> {
  return apiFetch("/nodes", { method: "POST", body: JSON.stringify(data) })
}

export async function updateNode(id: string, data: Partial<KBNode>): Promise<KBNode> {
  return apiFetch(`/nodes/${id}`, { method: "PATCH", body: JSON.stringify(data) })
}

export async function deleteNode(id: string): Promise<void> {
  await apiFetch(`/nodes/${id}`, { method: "DELETE" })
}

export async function searchNodes(q: string, limit = 20, offset = 0): Promise<SearchOut> {
  const params = new URLSearchParams({ q, limit: String(limit), offset: String(offset) })
  return apiFetch(`/search?${params}`)
}

export async function fetchGraphOverview(limit = 100): Promise<GraphData> {
  return apiFetch(`/graph/overview?limit=${limit}`)
}

export async function fetchNeighborhood(nodeId: string, hops = 1): Promise<GraphData> {
  return apiFetch(`/graph/neighborhood/${nodeId}?hops=${hops}`)
}

export async function fetchAdminStats(): Promise<AdminStats> {
  return apiFetch("/admin/stats")
}

export async function upsertDailyLog(date: string, body: string): Promise<KBNode> {
  return apiFetch("/daily-logs", { method: "POST", body: JSON.stringify({ date, body }) })
}

export async function fetchDailyLog(date: string): Promise<KBNode> {
  return apiFetch(`/daily-logs/${date}`)
}
