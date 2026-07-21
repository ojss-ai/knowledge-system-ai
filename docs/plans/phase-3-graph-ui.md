# Phase 3 — Graph UI

**Goal:** Build the Next.js 14 App Router frontend: BFF auth (httpOnly cookies), a Sigma.js + graphology interactive graph explorer, node detail/editor, search page, daily log page, and a bulk-upload stub. End-to-end Playwright tests confirm the full stack.

**Architecture refs:** ADR-006 (Sigma.js v3 + graphology), ADR-008 (JWT via BFF, no tokens in localStorage)

**Required skills (read before any task):**
- `kb-conventions`
- `kb-tdd-workflow`
- `kb-frontend-graph` — BFF pattern, one graphology instance, FA2 in web worker, progressive loading

**Exit criteria:**
- [ ] All tasks checked
- [ ] `npm run build` in `frontend/` exits 0
- [ ] `npx vitest run` green
- [ ] `npx playwright test` green (3 e2e tests)
- [ ] `npm run lint` (ESLint + tsc --noEmit) clean
- [ ] Graph canvas renders in browser without console errors

---

## Task 1 — Next.js 14 scaffold

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/next.config.mjs` <!-- [plan-fix] was next.config.ts; Next 14.2.3 rejects TS config ("Configuring Next.js via 'next.config.ts' is not supported") — TS config support only landed in Next 15 -->
- Create: `frontend/.eslintrc.json`
- Create: `frontend/tailwind.config.ts`
- Create: `frontend/postcss.config.js`

### Steps

- [x] **1.1** Write a smoke test first:

```typescript
// frontend/tests/unit/smoke.test.ts
// [plan-fix] added explicit vitest imports — vitest globals are not enabled
// (no vitest config), and Task 2's test already uses explicit imports
import { describe, it, expect } from "vitest"

describe("scaffold", () => {
  it("environment is Node", () => {
    expect(typeof process).toBe("object")
  })
})
```

- [x] **1.2** Create `package.json`:

```json
{
  "name": "kb-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint && tsc --noEmit",
    "test": "vitest run",
    "test:e2e": "playwright test"
  },
  "dependencies": {
    "next": "14.2.3",
    "react": "^18",
    "react-dom": "^18",
    "@tanstack/react-query": "^5.28.6",
    "zustand": "^4.5.2",
    "graphology": "^0.25.4",
    "graphology-layout-forceatlas2": "^0.10.1",
    "sigma": "^3.0.0",
    "graphology-types": "^0.24.7",
    "graphology-utils": "^2.5.2",
    "graphology-layout": "^0.6.1",
    "jose": "^5.2.4",
    "clsx": "^2.1.0"
  },
  "devDependencies": {
    "typescript": "^5",
    "@types/node": "^20",
    "@types/react": "^18",
    "@types/react-dom": "^18",
    "tailwindcss": "^3.4.1",
    "autoprefixer": "^10.0.1",
    "postcss": "^8",
    "eslint": "^8",
    "eslint-config-next": "14.2.3",
    "vitest": "^1.4.0",
    "@vitest/coverage-v8": "^1.4.0",
    "@playwright/test": "^1.43.0",
    "@testing-library/react": "^15.0.2",
    "@testing-library/jest-dom": "^6.4.2",
    "jsdom": "^24.0.0"
  }
}
```

- [x] **1.3** Create `tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2017",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": true,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "paths": { "@/*": ["./src/*"] }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

- [x] **1.4** Create `next.config.mjs` <!-- [plan-fix] see Files note: Next 14 cannot load next.config.ts -->:

```javascript
// frontend/next.config.mjs

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  experimental: { serverActions: { allowedOrigins: ["localhost:3000"] } },
  async rewrites() {
    return [
      // BFF: proxy /api/v1/* to FastAPI (in dev)
      {
        source: "/api/v1/:path*",
        destination: `${process.env.API_BASE_URL ?? "http://localhost:8000"}/api/v1/:path*`,
      },
    ]
  },
}

export default config
```

- [x] **1.5** Create `tailwind.config.ts`:

```typescript
import type { Config } from "tailwindcss"

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: { extend: {} },
  plugins: [],
}

export default config
```

- [x] **1.6** Create `postcss.config.js`:
```javascript
module.exports = { plugins: { tailwindcss: {}, autoprefixer: {} } }
```

- [x] **1.7** Create `.eslintrc.json`:
```json
{
  "extends": ["next/core-web-vitals"],
  "rules": {
    "no-console": "warn",
    "@typescript-eslint/no-explicit-any": "error"
  }
}
```

- [x] **1.8** Install and run smoke test:
```bash
cd frontend && npm install
npx vitest run tests/unit/smoke.test.ts
# Expected: 1 passed
```

- [x] **1.9** Commit:
```
chore(frontend): Next.js 14 scaffold — package.json, tsconfig, next.config, tailwind
```

---

## Task 2 — API client layer

**Files:**
- Create: `frontend/src/lib/api.ts`
- Create: `frontend/src/lib/types.ts`
- Create: `frontend/tests/unit/api.test.ts`
- Create: `frontend/vitest.config.ts` <!-- [plan-fix] added: test imports "@/lib/api" but vitest does not read tsconfig paths; config maps "@" → ./src -->

### Steps

- [x] **2.1** Write the failing test:

```typescript
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
```

- [x] **2.2** Create `types.ts`:

```typescript
// frontend/src/lib/types.ts
export type Visibility = "private" | "public" | "shared"
export type NodeType = "note" | "daily_log" | "file" | "code_file" | "code_symbol" | "confluence_page"

// [plan-fix] dropped deleted_at — backend NodeOut (backend/app/schemas/node.py)
// does not expose it; soft-deleted nodes are never returned.
export interface KBNode {
  id: string
  owner_id: string
  title: string
  body: string
  node_type: NodeType
  visibility: Visibility
  source: string | null
  source_ref: string | null
  meta: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface NodeListOut {
  items: KBNode[]
  total: number
  offset: number
  limit: number
}

export interface SearchResultItem {
  id: string
  title: string
  node_type: string
  visibility: string
  updated_at: string | null
  score: number
}

export interface SearchOut {
  items: SearchResultItem[]
  total: number
  query: string
}

export interface GraphData {
  // [plan-fix] visibility optional — GET /graph/overview omits it
  // (graph_service.get_overview returns only id/title/node_type)
  nodes: { id: string; title: string; node_type: string; visibility?: string }[]
  edges: { source: string; target: string; label: string }[]
}
```

- [x] **2.3** Create `api.ts`:

```typescript
// frontend/src/lib/api.ts
import type { KBNode, NodeListOut, SearchOut, GraphData } from "./types"

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

export async function upsertDailyLog(date: string, body: string): Promise<KBNode> {
  return apiFetch("/daily-logs", { method: "POST", body: JSON.stringify({ date, body }) })
}

export async function fetchDailyLog(date: string): Promise<KBNode> {
  return apiFetch(`/daily-logs/${date}`)
}
```

- [x] **2.4** Run tests:
```bash
cd frontend && npx vitest run tests/unit/api.test.ts
# Expected: 2 passed
```

- [x] **2.5** Commit:
```
feat(frontend): API client layer with typed wrappers for all endpoints
```

---

## Task 3 — BFF auth route handlers + middleware

**Files:**
- Create: `frontend/src/app/api/auth/login/route.ts`
- Create: `frontend/src/app/api/auth/logout/route.ts`
- Create: `frontend/src/app/api/auth/me/route.ts`
- Create: `frontend/src/middleware.ts`
- Create: `frontend/tests/unit/auth.test.ts`

### Steps

- [ ] **3.1** Write failing tests:

```typescript
// frontend/tests/unit/auth.test.ts
import { describe, it, expect } from "vitest"
import { parseAccessToken } from "@/lib/auth"

describe("parseAccessToken", () => {
  it("returns null for empty string", () => {
    expect(parseAccessToken("")).toBeNull()
  })

  it("returns null for malformed token", () => {
    expect(parseAccessToken("not.a.token")).toBeNull()
  })
})
```

- [ ] **3.2** Create `frontend/src/lib/auth.ts`:

```typescript
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
```

- [ ] **3.3** Create BFF login route:

```typescript
// frontend/src/app/api/auth/login/route.ts
import { NextRequest, NextResponse } from "next/server"

export async function POST(req: NextRequest) {
  const { email, password } = await req.json()
  const apiRes = await fetch(
    `${process.env.API_BASE_URL ?? "http://localhost:8000"}/api/v1/auth/login`,
    {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: email, password }),
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
```

- [ ] **3.4** Create BFF logout route:

```typescript
// frontend/src/app/api/auth/logout/route.ts
import { NextResponse } from "next/server"

export async function POST() {
  const res = NextResponse.json({ ok: true })
  res.cookies.delete("access_token")
  res.cookies.delete("refresh_token")
  return res
}
```

- [ ] **3.5** Create BFF me route (proxies to FastAPI with access_token from cookie):

```typescript
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
```

- [ ] **3.6** Create middleware (protect all non-auth pages):

```typescript
// frontend/src/middleware.ts
import { NextRequest, NextResponse } from "next/server"

const PUBLIC_PATHS = ["/login", "/api/auth"]

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) return NextResponse.next()
  const token = req.cookies.get("access_token")?.value
  if (!token) {
    return NextResponse.redirect(new URL("/login", req.url))
  }
  return NextResponse.next()
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
}
```

- [ ] **3.7** Run tests:
```bash
cd frontend && npx vitest run tests/unit/auth.test.ts
# Expected: 2 passed
```

- [ ] **3.8** Commit:
```
feat(frontend): BFF auth — login/logout/me routes + middleware cookie guard
```

---

## Task 4 — graphStyle.ts + GraphCanvas component

**Files:**
- Create: `frontend/src/lib/graphStyle.ts`
- Create: `frontend/src/lib/graphStore.ts`
- Create: `frontend/src/components/GraphCanvas.tsx`
- Create: `frontend/tests/unit/graphStyle.test.ts`

### Steps

- [ ] **4.1** Write the failing test:

```typescript
// frontend/tests/unit/graphStyle.test.ts
import { describe, it, expect } from "vitest"
import { nodeColor, nodeSize, edgeColor } from "@/lib/graphStyle"

describe("graphStyle", () => {
  it("note node has a defined color", () => {
    expect(nodeColor("note")).toBeTruthy()
  })
  it("daily_log node has a distinct color from note", () => {
    expect(nodeColor("daily_log")).not.toBe(nodeColor("note"))
  })
  it("node size scales with degree", () => {
    expect(nodeSize(10)).toBeGreaterThan(nodeSize(1))
  })
  it("edge color returns a string", () => {
    expect(typeof edgeColor("LINKS_TO")).toBe("string")
  })
})
```

- [ ] **4.2** Create `graphStyle.ts`:

```typescript
// frontend/src/lib/graphStyle.ts
/** Single source of truth for graph visual properties. Never put colors inline. */

const NODE_COLORS: Record<string, string> = {
  note: "#6366f1",          // indigo
  daily_log: "#10b981",     // emerald
  file: "#f59e0b",          // amber
  code_file: "#3b82f6",     // blue
  code_symbol: "#8b5cf6",   // violet
  confluence_page: "#06b6d4", // cyan
  default: "#94a3b8",       // slate
}

const EDGE_COLORS: Record<string, string> = {
  LINKS_TO: "#6366f1",
  SIMILAR_TO: "#10b981",
  CONTAINS: "#f59e0b",
  DEFINED_IN: "#3b82f6",
  CALLS: "#8b5cf6",
  default: "#94a3b8",
}

export function nodeColor(nodeType: string): string {
  return NODE_COLORS[nodeType] ?? NODE_COLORS.default
}

export function nodeSize(degree: number): number {
  return Math.min(4 + Math.sqrt(degree) * 2, 20)
}

export function edgeColor(label: string): string {
  return EDGE_COLORS[label] ?? EDGE_COLORS.default
}
```

- [ ] **4.3** Create `graphStore.ts` (Zustand — graph UI state only):

```typescript
// frontend/src/lib/graphStore.ts
import { create } from "zustand"

interface GraphStore {
  selectedNodeId: string | null
  hoveredNodeId: string | null
  setSelectedNode: (id: string | null) => void
  setHoveredNode: (id: string | null) => void
  expandedNodeIds: Set<string>
  markExpanded: (id: string) => void
}

export const useGraphStore = create<GraphStore>((set) => ({
  selectedNodeId: null,
  hoveredNodeId: null,
  expandedNodeIds: new Set(),
  setSelectedNode: (id) => set({ selectedNodeId: id }),
  setHoveredNode: (id) => set({ hoveredNodeId: id }),
  markExpanded: (id) =>
    set((s) => ({ expandedNodeIds: new Set([...s.expandedNodeIds, id]) })),
}))
```

- [ ] **4.4** Create `GraphCanvas.tsx` (Sigma.js v3 + FA2 web worker):

```typescript
// frontend/src/components/GraphCanvas.tsx
"use client"

import { useEffect, useRef, useCallback } from "react"
import Graph from "graphology"
import { Sigma } from "sigma"
import forceAtlas2 from "graphology-layout-forceatlas2"
import { nodeColor, nodeSize, edgeColor } from "@/lib/graphStyle"
import { useGraphStore } from "@/lib/graphStore"
import type { GraphData } from "@/lib/types"

interface GraphCanvasProps {
  data: GraphData
  onNodeClick?: (nodeId: string) => void
  className?: string
}

export default function GraphCanvas({ data, onNodeClick, className }: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  const graphRef = useRef<Graph | null>(null)
  const { setSelectedNode, setHoveredNode } = useGraphStore()

  const buildGraph = useCallback((d: GraphData): Graph => {
    // Always reuse / reinitialise the same graphology instance (kb-frontend-graph rule)
    const g = graphRef.current ?? new Graph({ multi: false, type: "mixed" })
    g.clear()

    for (const node of d.nodes) {
      g.addNode(node.id, {
        label: node.title,
        x: Math.random(),
        y: Math.random(),
        size: nodeSize(0),
        color: nodeColor(node.node_type),
        nodeType: node.node_type,
      })
    }

    for (const edge of d.edges) {
      if (!g.hasEdge(edge.source, edge.target)) {
        try {
          g.addEdge(edge.source, edge.target, {
            label: edge.label,
            color: edgeColor(edge.label),
            size: 1,
          })
        } catch {
          // Ignore duplicate edge errors (multi:false)
        }
      }
    }

    // Update degree-based sizes
    g.forEachNode((id) => {
      g.setNodeAttribute(id, "size", nodeSize(g.degree(id)))
    })

    return g
  }, [])

  useEffect(() => {
    if (!containerRef.current) return

    const g = buildGraph(data)
    graphRef.current = g

    // ForceAtlas2 layout (synchronous for small graphs; move to worker for >500 nodes)
    if (g.order > 0) {
      forceAtlas2.assign(g, { iterations: 50, settings: { gravity: 1, scalingRatio: 2 } })
    }

    // Dispose existing Sigma instance before creating a new one
    sigmaRef.current?.kill()
    sigmaRef.current = new Sigma(g, containerRef.current, {
      renderEdgeLabels: false,
      defaultEdgeType: "arrow",
      allowInvalidContainer: true,
    })

    const sigma = sigmaRef.current

    sigma.on("clickNode", ({ node }) => {
      setSelectedNode(node)
      onNodeClick?.(node)
    })
    sigma.on("enterNode", ({ node }) => setHoveredNode(node))
    sigma.on("leaveNode", () => setHoveredNode(null))

    return () => {
      sigma.kill()
      sigmaRef.current = null
    }
  }, [data, buildGraph, setSelectedNode, setHoveredNode, onNodeClick])

  return (
    <div
      ref={containerRef}
      className={className ?? "w-full h-full bg-gray-950 rounded-lg"}
      style={{ minHeight: 400 }}
    />
  )
}
```

- [ ] **4.5** Run tests:
```bash
cd frontend && npx vitest run tests/unit/graphStyle.test.ts
# Expected: 4 passed
```

- [ ] **4.6** Commit:
```
feat(frontend): graphStyle, graphStore (Zustand), GraphCanvas with Sigma.js v3 + FA2
```

---

## Task 5 — App layout + pages

**Files:**
- Create: `frontend/src/app/layout.tsx`
- Create: `frontend/src/app/login/page.tsx`
- Create: `frontend/src/app/graph/page.tsx`
- Create: `frontend/src/app/nodes/[id]/page.tsx`
- Create: `frontend/src/app/search/page.tsx`
- Create: `frontend/src/app/daily/page.tsx`
- Create: `frontend/src/app/upload/page.tsx`
- Create: `frontend/src/components/Providers.tsx`
- Create: `frontend/src/components/Sidebar.tsx`
- Create: `frontend/tests/unit/pages.test.tsx`

### Steps

- [ ] **5.1** Write failing test:

```typescript
// frontend/tests/unit/pages.test.tsx
import { describe, it, expect } from "vitest"

// Sanity: pages are importable (no syntax errors)
describe("page modules", () => {
  it("login page exists as a module", async () => {
    // Dynamic import — checks the file parses without error
    const mod = await import("@/app/login/page")
    expect(typeof mod.default).toBe("function")
  })
})
```

- [ ] **5.2** Create root layout:

```typescript
// frontend/src/app/layout.tsx
import type { Metadata } from "next"
import { Inter } from "next/font/google"
import "./globals.css"
import Providers from "@/components/Providers"

const inter = Inter({ subsets: ["latin"] })

export const metadata: Metadata = {
  title: "Knowledge Base",
  description: "Self-hosted graph knowledge management",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${inter.className} bg-gray-950 text-gray-100 min-h-screen`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  )
}
```

- [ ] **5.3** Create `globals.css`:
```css
/* frontend/src/app/globals.css */
@tailwind base;
@tailwind components;
@tailwind utilities;
```

- [ ] **5.4** Create Providers (TanStack Query):

```typescript
// frontend/src/components/Providers.tsx
"use client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { useState } from "react"

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
  }))
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}
```

- [ ] **5.5** Create Login page:

```typescript
// frontend/src/app/login/page.tsx
"use client"
import { useState } from "react"
import { useRouter } from "next/navigation"

export default function LoginPage() {
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const router = useRouter()

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    })
    if (res.ok) {
      router.push("/graph")
    } else {
      setError("Invalid credentials")
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <form onSubmit={handleSubmit} className="bg-gray-900 p-8 rounded-xl w-80 space-y-4">
        <h1 className="text-xl font-bold text-center">Knowledge Base</h1>
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <input
          type="email" value={email} onChange={(e) => setEmail(e.target.value)}
          placeholder="Email" required
          className="w-full bg-gray-800 rounded p-2 text-sm outline-none"
        />
        <input
          type="password" value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="Password" required
          className="w-full bg-gray-800 rounded p-2 text-sm outline-none"
        />
        <button type="submit" className="w-full bg-indigo-600 hover:bg-indigo-500 rounded p-2 font-medium">
          Sign in
        </button>
      </form>
    </div>
  )
}
```

- [ ] **5.6** Create Sidebar:

```typescript
// frontend/src/components/Sidebar.tsx
"use client"
import Link from "next/link"
import { usePathname } from "next/navigation"
import clsx from "clsx"

const LINKS = [
  { href: "/graph", label: "Graph" },
  { href: "/search", label: "Search" },
  { href: "/daily", label: "Daily Log" },
  { href: "/upload", label: "Upload" },
]

export default function Sidebar() {
  const pathname = usePathname()
  return (
    <aside className="w-52 bg-gray-900 h-screen flex flex-col p-4 gap-1 shrink-0">
      <span className="text-indigo-400 font-bold text-lg mb-4">KB</span>
      {LINKS.map((l) => (
        <Link key={l.href} href={l.href}
          className={clsx(
            "px-3 py-2 rounded text-sm transition-colors",
            pathname.startsWith(l.href) ? "bg-indigo-600 text-white" : "text-gray-400 hover:bg-gray-800"
          )}>
          {l.label}
        </Link>
      ))}
    </aside>
  )
}
```

- [ ] **5.7** Create Graph explorer page:

```typescript
// frontend/src/app/graph/page.tsx
"use client"
import { useQuery } from "@tanstack/react-query"
import { useRouter } from "next/navigation"
import dynamic from "next/dynamic"
import Sidebar from "@/components/Sidebar"
import { fetchGraphOverview } from "@/lib/api"

// Load GraphCanvas only on client (Sigma requires DOM)
const GraphCanvas = dynamic(() => import("@/components/GraphCanvas"), { ssr: false })

export default function GraphPage() {
  const router = useRouter()
  const { data, isLoading, error } = useQuery({
    queryKey: ["graph-overview"],
    queryFn: () => fetchGraphOverview(100),
  })

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 p-4 overflow-hidden">
        <h1 className="text-lg font-semibold mb-3">Knowledge Graph</h1>
        {isLoading && <p className="text-gray-400">Loading graph…</p>}
        {error && <p className="text-red-400">Failed to load graph</p>}
        {data && (
          <div className="h-[calc(100vh-6rem)]">
            <GraphCanvas
              data={data}
              onNodeClick={(id) => router.push(`/nodes/${id}`)}
            />
          </div>
        )}
      </main>
    </div>
  )
}
```

- [ ] **5.8** Create Node detail page:

```typescript
// frontend/src/app/nodes/[id]/page.tsx
"use client"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useParams, useRouter } from "next/navigation"
import { useState } from "react"
import Sidebar from "@/components/Sidebar"
import { fetchNode, updateNode, deleteNode } from "@/lib/api"
import dynamic from "next/dynamic"

const GraphCanvas = dynamic(() => import("@/components/GraphCanvas"), { ssr: false })

export default function NodePage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [body, setBody] = useState("")

  const { data: node, isLoading } = useQuery({
    queryKey: ["node", id],
    queryFn: () => fetchNode(id),
    onSuccess: (n) => setBody(n.body),
  })

  const update = useMutation({
    mutationFn: () => updateNode(id, { body }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["node", id] }); setEditing(false) },
  })

  const remove = useMutation({
    mutationFn: () => deleteNode(id),
    onSuccess: () => router.push("/graph"),
  })

  if (isLoading) return <div className="flex h-screen"><Sidebar /><p className="m-auto text-gray-400">Loading…</p></div>
  if (!node) return <div className="flex h-screen"><Sidebar /><p className="m-auto text-red-400">Not found</p></div>

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 p-6 overflow-auto max-w-3xl">
        <h1 className="text-2xl font-bold mb-1">{node.title}</h1>
        <p className="text-xs text-gray-500 mb-4">
          {node.node_type} · {node.visibility} · updated {new Date(node.updated_at).toLocaleDateString()}
        </p>
        {editing ? (
          <div className="space-y-2">
            <textarea
              value={body} onChange={(e) => setBody(e.target.value)}
              className="w-full h-64 bg-gray-800 rounded p-3 text-sm font-mono resize-none"
            />
            <div className="flex gap-2">
              <button onClick={() => update.mutate()} className="bg-indigo-600 px-4 py-1 rounded text-sm">Save</button>
              <button onClick={() => setEditing(false)} className="bg-gray-700 px-4 py-1 rounded text-sm">Cancel</button>
            </div>
          </div>
        ) : (
          <div>
            <pre className="whitespace-pre-wrap text-sm text-gray-300 mb-4">{node.body}</pre>
            <div className="flex gap-2">
              <button onClick={() => setEditing(true)} className="bg-gray-700 px-4 py-1 rounded text-sm">Edit</button>
              <button onClick={() => remove.mutate()} className="bg-red-700 px-4 py-1 rounded text-sm">Delete</button>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
```

- [ ] **5.9** Create Search page:

```typescript
// frontend/src/app/search/page.tsx
"use client"
import { useState } from "react"
import Link from "next/link"
import Sidebar from "@/components/Sidebar"
import { searchNodes } from "@/lib/api"
import type { SearchResultItem } from "@/lib/types"

export default function SearchPage() {
  const [q, setQ] = useState("")
  const [results, setResults] = useState<SearchResultItem[]>([])
  const [loading, setLoading] = useState(false)

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault()
    if (!q.trim()) return
    setLoading(true)
    const data = await searchNodes(q)
    setResults(data.items)
    setLoading(false)
  }

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 p-6 overflow-auto">
        <h1 className="text-lg font-semibold mb-4">Search</h1>
        <form onSubmit={handleSearch} className="flex gap-2 mb-6">
          <input
            value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="Search knowledge base…"
            className="flex-1 bg-gray-800 rounded px-3 py-2 text-sm outline-none"
          />
          <button type="submit" className="bg-indigo-600 px-4 rounded text-sm">Search</button>
        </form>
        {loading && <p className="text-gray-400">Searching…</p>}
        <ul className="space-y-2">
          {results.map((r) => (
            <li key={r.id}>
              <Link href={`/nodes/${r.id}`}
                className="block bg-gray-900 rounded p-3 hover:bg-gray-800 transition-colors">
                <p className="font-medium">{r.title}</p>
                <p className="text-xs text-gray-500">{r.node_type} · score {r.score.toFixed(3)}</p>
              </Link>
            </li>
          ))}
        </ul>
      </main>
    </div>
  )
}
```

- [ ] **5.10** Create Daily Log page:

```typescript
// frontend/src/app/daily/page.tsx
"use client"
import { useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import Sidebar from "@/components/Sidebar"
import { fetchDailyLog, upsertDailyLog } from "@/lib/api"

function todayISO() {
  return new Date().toISOString().slice(0, 10)
}

export default function DailyPage() {
  const today = todayISO()
  const qc = useQueryClient()
  const [body, setBody] = useState("")
  const [saved, setSaved] = useState(false)

  const { data: log } = useQuery({
    queryKey: ["daily-log", today],
    queryFn: () => fetchDailyLog(today).catch(() => null),
    onSuccess: (d) => { if (d) setBody(d.body) },
  })

  const save = useMutation({
    mutationFn: () => upsertDailyLog(today, body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["daily-log", today] }); setSaved(true); setTimeout(() => setSaved(false), 2000) },
  })

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 p-6 flex flex-col max-w-3xl">
        <h1 className="text-lg font-semibold mb-1">Daily Log</h1>
        <p className="text-xs text-gray-500 mb-4">{today}</p>
        <textarea
          value={body} onChange={(e) => setBody(e.target.value)}
          placeholder="What did you work on today? Use [[Node Title]] to link nodes."
          className="flex-1 bg-gray-900 rounded p-4 text-sm font-mono resize-none outline-none"
        />
        <div className="flex items-center gap-3 mt-3">
          <button onClick={() => save.mutate()} className="bg-indigo-600 px-4 py-2 rounded text-sm">
            Save
          </button>
          {saved && <span className="text-green-400 text-sm">Saved!</span>}
        </div>
      </main>
    </div>
  )
}
```

- [ ] **5.11** Create Upload stub page:

```typescript
// frontend/src/app/upload/page.tsx
"use client"
import Sidebar from "@/components/Sidebar"

export default function UploadPage() {
  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 p-6">
        <h1 className="text-lg font-semibold mb-4">Upload</h1>
        <p className="text-gray-400">Bulk Markdown upload — implemented in Phase 4.</p>
      </main>
    </div>
  )
}
```

- [ ] **5.12** Run tests:
```bash
cd frontend && npx vitest run tests/unit/pages.test.tsx
# Expected: 1 passed
npm run build
# Expected: exit 0
```

- [ ] **5.13** Commit:
```
feat(frontend): layout, login, graph explorer, node detail, search, daily-log, upload stub pages
```

---

## Task 6 — Playwright e2e tests

**Files:**
- Create: `frontend/playwright.config.ts`
- Create: `frontend/tests/e2e/auth.spec.ts`
- Create: `frontend/tests/e2e/graph.spec.ts`
- Create: `frontend/tests/e2e/search.spec.ts`

### Steps

- [ ] **6.1** Create Playwright config:

```typescript
// frontend/playwright.config.ts
import { defineConfig, devices } from "@playwright/test"

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  retries: 1,
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
})
```

- [ ] **6.2** Auth e2e test:

```typescript
// frontend/tests/e2e/auth.spec.ts
import { test, expect } from "@playwright/test"

test("unauthenticated user is redirected to /login", async ({ page }) => {
  await page.goto("/graph")
  await expect(page).toHaveURL(/\/login/)
})

test("login with valid credentials navigates to graph", async ({ page }) => {
  // Assumes seed admin exists (from Phase 0, Task 10)
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@kb.local")
  await page.fill('input[type="password"]', "admin1234")
  await page.click('button[type="submit"]')
  await expect(page).toHaveURL(/\/graph/, { timeout: 10_000 })
})

test("login with wrong password shows error", async ({ page }) => {
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@kb.local")
  await page.fill('input[type="password"]', "wrongpassword")
  await page.click('button[type="submit"]')
  await expect(page.locator("text=Invalid credentials")).toBeVisible()
})
```

- [ ] **6.3** Graph e2e test:

```typescript
// frontend/tests/e2e/graph.spec.ts
import { test, expect } from "@playwright/test"

test.beforeEach(async ({ page }) => {
  // Log in first
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@kb.local")
  await page.fill('input[type="password"]', "admin1234")
  await page.click('button[type="submit"]')
  await page.waitForURL(/\/graph/)
})

test("graph page renders canvas element", async ({ page }) => {
  await expect(page.locator("canvas")).toBeVisible({ timeout: 10_000 })
})

test("graph page shows sidebar links", async ({ page }) => {
  await expect(page.locator("text=Search")).toBeVisible()
  await expect(page.locator("text=Daily Log")).toBeVisible()
})
```

- [ ] **6.4** Search e2e test:

```typescript
// frontend/tests/e2e/search.spec.ts
import { test, expect } from "@playwright/test"

test.beforeEach(async ({ page }) => {
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@kb.local")
  await page.fill('input[type="password"]', "admin1234")
  await page.click('button[type="submit"]')
  await page.waitForURL(/\/graph/)
})

test("search page is accessible from sidebar", async ({ page }) => {
  await page.click("text=Search")
  await expect(page).toHaveURL(/\/search/)
  await expect(page.locator('input[placeholder*="Search"]')).toBeVisible()
})

test("search returns results or empty list", async ({ page }) => {
  await page.goto("/search")
  await page.fill('input[placeholder*="Search"]', "knowledge")
  await page.click('button[type="submit"]')
  // Either results appear or no error is shown
  await expect(page.locator("text=Searching…").or(page.locator("ul"))).toBeVisible({ timeout: 10_000 })
})
```

- [ ] **6.5** Run e2e tests (requires running stack):
```bash
cd frontend
npx playwright install chromium
# Start the stack first: cd .. && make up api
npx playwright test
# Expected: 3+ passed
```

- [ ] **6.6** Run final checks:
```bash
cd frontend
npm run lint    # ESLint + tsc --noEmit
npm run build   # Next.js production build
npx vitest run  # Unit tests
```

- [ ] **6.7** Commit:
```
feat(frontend): Playwright e2e tests — auth, graph, search
```

---

## Phase 3 exit gate

```bash
cd frontend
npm run lint            # ESLint + TypeScript clean
npm run build           # exits 0
npx vitest run          # unit tests pass
npx playwright test     # e2e tests pass

# Manual check:
# Open http://localhost:3000 — graph canvas renders without console errors
# Click a node — navigates to /nodes/:id
# Daily log saves and reloads
```

Update `docs/plans/README.md` — Phase 3 Status → `Done`.
