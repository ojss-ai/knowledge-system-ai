// frontend/src/app/admin/page.tsx
// [plan-fix]: plan used a raw fetch(); kb-conventions require the typed
// client (lib/api.ts). Role gating is server-side: the backend returns 403
// for non-admins (client-only auth checks for admin UI are a red flag).
"use client"
import { useQuery } from "@tanstack/react-query"
import Sidebar from "@/components/Sidebar"
import { fetchAdminStats } from "@/lib/api"

export default function AdminPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["admin-stats"],
    queryFn: fetchAdminStats,
  })

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 p-6 overflow-auto">
        <h1 className="text-xl font-bold mb-6">Admin Dashboard</h1>
        {isLoading && <p className="text-gray-400">Loading…</p>}
        {error && <p className="text-red-400">Access denied or error loading stats</p>}
        {data && (
          <div className="grid grid-cols-3 gap-4">
            {[
              { label: "Total Users", value: data.total_users },
              { label: "Active Users", value: data.active_users },
              { label: "Total Nodes", value: data.total_nodes },
              { label: "Total Chunks", value: data.total_chunks },
              { label: "Audit Events", value: data.total_audit_events },
            ].map((stat) => (
              <div key={stat.label} className="bg-gray-900 rounded-xl p-5">
                <p className="text-xs text-gray-500 uppercase tracking-wider">{stat.label}</p>
                <p className="text-3xl font-bold mt-1">{stat.value.toLocaleString()}</p>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
