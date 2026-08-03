// frontend/src/middleware.ts
import { NextRequest, NextResponse } from "next/server"
import { requiresLoginRedirect } from "@/lib/routes"

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl
  if (!requiresLoginRedirect(pathname)) return NextResponse.next()
  const token = req.cookies.get("access_token")?.value
  if (!token) {
    return NextResponse.redirect(new URL("/login", req.url))
  }
  return NextResponse.next()
}

export const config = {
  // api/ is excluded: unauthenticated API calls must get a JSON 401 from the
  // BFF proxy / backend, never a 307 HTML redirect to /login. Only page
  // navigations are guarded here. requiresLoginRedirect() repeats the /api
  // exclusion defensively (the matcher can't be unit-tested; the helper can).
  matcher: ["/((?!api/|_next/static|_next/image|favicon.ico).*)"],
}
