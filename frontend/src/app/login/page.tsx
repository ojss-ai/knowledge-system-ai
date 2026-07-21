// frontend/src/app/login/page.tsx
"use client"
import { useState } from "react"
import { useRouter } from "next/navigation"

export default function LoginPage() {
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const router = useRouter()

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    })
    if (res.ok) {
      router.push("/graph")
    } else {
      setError("Invalid credentials")
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <form onSubmit={handleSubmit} className="bg-gray-900 p-8 rounded-xl w-80 space-y-4">
        <h1 className="text-xl font-bold text-center">Knowledge Base</h1>
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <input
          type="email" value={email} onChange={(e) => setEmail(e.target.value)}
          placeholder="Email" required
          className="w-full bg-gray-800 rounded p-2 text-sm outline-none"
        />
        <input
          type="password" value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="Password" required
          className="w-full bg-gray-800 rounded p-2 text-sm outline-none"
        />
        <button type="submit" className="w-full bg-indigo-600 hover:bg-indigo-500 rounded p-2 font-medium">
          Sign in
        </button>
      </form>
    </div>
  )
}
