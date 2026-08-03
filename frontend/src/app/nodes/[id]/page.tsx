// frontend/src/app/nodes/[id]/page.tsx
"use client"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useParams, useRouter } from "next/navigation"
import { useEffect, useState } from "react"
import Sidebar from "@/components/Sidebar"
import { fetchNode, updateNode, deleteNode } from "@/lib/api"

export default function NodePage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [body, setBody] = useState("")

  const { data: node, isLoading } = useQuery({
    queryKey: ["node", id],
    queryFn: () => fetchNode(id),
  })

  // [plan-fix] TanStack Query v5 removed useQuery onSuccess — sync via effect
  useEffect(() => {
    if (node) setBody(node.body)
  }, [node])

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
