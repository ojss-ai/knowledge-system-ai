// frontend/src/app/daily/page.tsx
"use client"
import { useEffect, useState } from "react"
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
  })

  // [plan-fix] TanStack Query v5 removed useQuery onSuccess — sync via effect
  useEffect(() => {
    if (log) setBody(log.body)
  }, [log])

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
