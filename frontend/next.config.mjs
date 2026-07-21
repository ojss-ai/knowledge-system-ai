// frontend/next.config.mjs
// [plan-fix] Plan specified next.config.ts, but Next.js 14.2.3 does not support
// TypeScript config files (added in Next 15): "Configuring Next.js via
// 'next.config.ts' is not supported." Same content, .mjs with JSDoc typing.
// [3.R] Rewrite to FastAPI removed: a bare rewrite forwards cookies but the
// backend only accepts Authorization: Bearer (HTTPBearer in deps.py), so every
// proxied call 401'd. The BFF route handler src/app/api/v1/[...path]/route.ts
// now proxies and attaches the Bearer token from the httpOnly cookie (ADR-008).

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  experimental: { serverActions: { allowedOrigins: ["localhost:3000"] } },
}

export default config
