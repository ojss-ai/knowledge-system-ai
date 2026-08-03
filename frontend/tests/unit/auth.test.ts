// frontend/tests/unit/auth.test.ts
import { describe, it, expect } from "vitest"
import { parseAccessToken } from "@/lib/auth"

describe("parseAccessToken", () => {
  it("returns null for empty string", () => {
    expect(parseAccessToken("")).toBeNull()
  })

  it("returns null for malformed token", () => {
    expect(parseAccessToken("not.a.token")).toBeNull()
  })
})
