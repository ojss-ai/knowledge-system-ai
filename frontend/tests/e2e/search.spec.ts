// frontend/tests/e2e/search.spec.ts
import { test, expect } from "@playwright/test"

test.beforeEach(async ({ page }) => {
  // [plan-fix] admin@example.com — see auth.spec.ts
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@example.com")
  await page.fill('input[type="password"]', "admin1234")
  await page.click('button[type="submit"]')
  await page.waitForURL(/\/graph/)
})

test("search page is accessible from sidebar", async ({ page }) => {
  await page.click("text=Search")
  await expect(page).toHaveURL(/\/search/)
  await expect(page.locator('input[placeholder*="Search"]')).toBeVisible()
})

test("search returns results or empty list", async ({ page }) => {
  await page.goto("/search")
  await page.fill('input[placeholder*="Search"]', "knowledge")
  await page.click('button[type="submit"]')
  // Either results appear or no error is shown. [plan-fix] .first(): both the
  // transient "Searching…" state and the <ul> can be visible at once, which
  // trips Playwright's strict mode on an .or() locator.
  await expect(
    page.locator("text=Searching…").or(page.locator("ul")).first()
  ).toBeVisible({ timeout: 10_000 })
})
