// frontend/tests/e2e/graph.spec.ts
import { test, expect } from "@playwright/test"

test.beforeEach(async ({ page }) => {
  // Log in first ([plan-fix] admin@example.com — see auth.spec.ts)
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@example.com")
  await page.fill('input[type="password"]', "admin1234")
  await page.click('button[type="submit"]')
  await page.waitForURL(/\/graph/)
})

test("graph page renders canvas element", async ({ page }) => {
  // [plan-fix] requires a live Neo4j: /api/v1/graph/overview 503s without it
  // and the page shows its error state instead of the canvas. Set
  // E2E_SKIP_NEO4J=1 to skip in Neo4j-less environments (sandbox); runs by
  // default on the Docker stack.
  test.skip(process.env.E2E_SKIP_NEO4J === "1", "requires Neo4j (graph overview 503s)")
  await expect(page.locator("canvas")).toBeVisible({ timeout: 10_000 })
})

test("graph page shows sidebar links", async ({ page }) => {
  await expect(page.locator("text=Search")).toBeVisible()
  await expect(page.locator("text=Daily Log")).toBeVisible()
})
