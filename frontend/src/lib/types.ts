// frontend/src/lib/types.ts
export type Visibility = "private" | "public" | "shared"
export type NodeType = "note" | "daily_log" | "file" | "code_file" | "code_symbol" | "confluence_page"

// [plan-fix] no deleted_at — backend NodeOut (backend/app/schemas/node.py) does
// not expose it; soft-deleted nodes are simply never returned.
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
  // [plan-fix] visibility is optional — GET /graph/overview omits it
  // (graph_service.get_overview returns only id/title/node_type);
  // neighborhood responses include it.
  nodes: { id: string; title: string; node_type: string; visibility?: string }[]
  edges: { source: string; target: string; label: string }[]
}
