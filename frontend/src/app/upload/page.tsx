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
