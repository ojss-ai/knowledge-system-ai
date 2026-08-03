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
