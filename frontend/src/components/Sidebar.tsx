// frontend/src/components/Sidebar.tsx
"use client"
import Link from "next/link"
import { usePathname } from "next/navigation"
import clsx from "clsx"

const LINKS = [
  { href: "/graph", label: "Graph" },
  { href: "/search", label: "Search" },
  { href: "/daily", label: "Daily Log" },
  { href: "/upload", label: "Upload" },
]

export default function Sidebar() {
  const pathname = usePathname()
  return (
    <aside className="w-52 bg-gray-900 h-screen flex flex-col p-4 gap-1 shrink-0">
      <span className="text-indigo-400 font-bold text-lg mb-4">KB</span>
      {LINKS.map((l) => (
        <Link key={l.href} href={l.href}
          className={clsx(
            "px-3 py-2 rounded text-sm transition-colors",
            pathname.startsWith(l.href) ? "bg-indigo-600 text-white" : "text-gray-400 hover:bg-gray-800"
          )}>
          {l.label}
        </Link>
      ))}
    </aside>
  )
}
