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
