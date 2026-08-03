// frontend/tests/unit/graphStyle.test.ts
import { describe, it, expect } from "vitest"
import { nodeColor, nodeSize, edgeColor } from "@/lib/graphStyle"

describe("graphStyle", () => {
  it("note node has a defined color", () => {
    expect(nodeColor("note")).toBeTruthy()
  })
  it("daily_log node has a distinct color from note", () => {
    expect(nodeColor("daily_log")).not.toBe(nodeColor("note"))
  })
  it("node size scales with degree", () => {
    expect(nodeSize(10)).toBeGreaterThan(nodeSize(1))
  })
  it("edge color returns a string", () => {
    expect(typeof edgeColor("LINKS_TO")).toBe("string")
  })
})
