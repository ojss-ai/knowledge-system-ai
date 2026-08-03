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
