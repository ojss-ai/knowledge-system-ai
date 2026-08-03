// frontend/tests/unit/smoke.test.ts
import { describe, it, expect } from "vitest"

describe("scaffold", () => {
  it("environment is Node", () => {
    expect(typeof process).toBe("object")
  })
})
