// [plan-fix] added: the plan's api.test.ts imports "@/lib/api" but no vitest
// config existed to resolve the tsconfig "@/*" alias — vitest does not read
// tsconfig paths on its own.
import { defineConfig } from "vitest/config"
import { fileURLToPath } from "node:url"

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    include: ["tests/unit/**/*.test.ts"],
  },
})
