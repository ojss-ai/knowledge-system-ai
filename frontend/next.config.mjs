// frontend/next.config.mjs
// [plan-fix] Plan specified next.config.ts, but Next.js 14.2.3 does not support
// TypeScript config files (added in Next 15): "Configuring Next.js via
// 'next.config.ts' is not supported." Same content, .mjs with JSDoc typing.

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  experimental: { serverActions: { allowedOrigins: ["localhost:3000"] } },
  async rewrites() {
    return [
      // BFF: proxy /api/v1/* to FastAPI (in dev)
      {
        source: "/api/v1/:path*",
        destination: `${process.env.API_BASE_URL ?? "http://localhost:8000"}/api/v1/:path*`,
      },
    ]
  },
}

export default config
