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
