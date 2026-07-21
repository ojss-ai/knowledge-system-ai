// frontend/src/components/GraphCanvas.tsx
"use client"

import { useEffect, useRef, useCallback } from "react"
import Graph from "graphology"
import { Sigma } from "sigma"
import forceAtlas2 from "graphology-layout-forceatlas2"
import FA2LayoutSupervisor from "graphology-layout-forceatlas2/worker"
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

    // [plan-fix] ForceAtlas2 runs in a web worker (kb-frontend-graph rule:
    // never run layout on the main thread) — plan originally ran it synchronously.
    // Start on build, stop after a bounded settle window, kill on cleanup.
    let layout: FA2LayoutSupervisor | null = null
    let layoutTimer: ReturnType<typeof setTimeout> | null = null
    if (g.order > 0) {
      layout = new FA2LayoutSupervisor(g, {
        settings: { ...forceAtlas2.inferSettings(g), gravity: 1, scalingRatio: 2 },
      })
      layout.start()
      layoutTimer = setTimeout(() => layout?.stop(), 3000)
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
      if (layoutTimer) clearTimeout(layoutTimer)
      layout?.kill()
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
