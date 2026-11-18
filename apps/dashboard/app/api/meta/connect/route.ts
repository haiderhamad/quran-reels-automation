import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { buildMetaOAuthUrl, resolveMetaRedirectOrigin } from "@/lib/meta-api";
import { createOAuthState, META_OAUTH_STATE_COOKIE } from "@/lib/meta-credentials";

export async function GET(request: NextRequest) {
  if (!(await getAdminSession())) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  try {
    const origin = resolveMetaRedirectOrigin(request.url);
    const redirectUri = `${origin}/api/meta/callback`;
    const state = createOAuthState();
    const response = NextResponse.redirect(buildMetaOAuthUrl(redirectUri, state));
    response.cookies.set(META_OAUTH_STATE_COOKIE, state, {
      httpOnly: true,
      secure: origin.startsWith("https://"),
      sameSite: "lax",
      path: "/api/meta/callback",
      maxAge: 600,
    });
    return response;
  } catch {
    return NextResponse.json({ error: "Meta connection is not configured." }, { status: 503 });
  }
}
