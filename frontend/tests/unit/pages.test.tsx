// frontend/tests/unit/pages.test.tsx
import { describe, it, expect } from "vitest"

// Sanity: pages are importable (no syntax errors)
describe("page modules", () => {
  it("login page exists as a module", async () => {
    // Dynamic import — checks the file parses without error
    const mod = await import("@/app/login/page")
    expect(typeof mod.default).toBe("function")
  })

  it("admin page exists as a module", async () => {
    const mod = await import("@/app/admin/page")
    expect(typeof mod.default).toBe("function")
  })
})
