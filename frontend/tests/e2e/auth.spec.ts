// frontend/tests/e2e/auth.spec.ts
import { test, expect } from "@playwright/test"

test("unauthenticated user is redirected to /login", async ({ page }) => {
  await page.goto("/graph")
  await expect(page).toHaveURL(/\/login/)
})

test("login with valid credentials navigates to graph", async ({ page }) => {
  // Assumes seed admin exists (from Phase 0, Task 10):
  //   python -m app.scripts.seed_admin admin@example.com admin1234
  // [plan-fix] plan used admin@kb.local, but the backend's EmailStr
  // (email-validator >= 2) rejects .local as a special-use reserved
  // domain — login would 422 before ever checking the password.
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@example.com")
  await page.fill('input[type="password"]', "admin1234")
  await page.click('button[type="submit"]')
  await expect(page).toHaveURL(/\/graph/, { timeout: 10_000 })
})

test("login with wrong password shows error", async ({ page }) => {
  await page.goto("/login")
  await page.fill('input[type="email"]', "admin@example.com")
  await page.fill('input[type="password"]', "wrongpassword")
  await page.click('button[type="submit"]')
  await expect(page.locator("text=Invalid credentials")).toBeVisible()
})
