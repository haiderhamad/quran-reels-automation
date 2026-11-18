import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { saveEncryptedMetaCredentials } from "@/lib/b2";
import { exchangeMetaCode, resolveMetaRedirectOrigin } from "@/lib/meta-api";
import {
  encryptMetaCredentials,
  META_OAUTH_STATE_COOKIE,
  verifyOAuthState,
} from "@/lib/meta-credentials";

function clearState(response: NextResponse): NextResponse {
  response.cookies.set(META_OAUTH_STATE_COOKIE, "", {
    httpOnly: true,
    secure: response.url.startsWith("https://"),
    sameSite: "lax",
    path: "/api/meta/callback",
    maxAge: 0,
  });
  return response;
}

export async function GET(request: NextRequest) {
  const origin = resolveMetaRedirectOrigin(request.url);
  const failure = (reason: string) =>
    clearState(NextResponse.redirect(`${origin}/settings?meta=error&reason=${encodeURIComponent(reason)}`));
  if (!(await getAdminSession())) return failure("unauthorized");
  const state = request.nextUrl.searchParams.get("state") || "";
  const expected = request.cookies.get(META_OAUTH_STATE_COOKIE)?.value || "";
  if (!verifyOAuthState(state, expected)) return failure("invalid_state");
  if (request.nextUrl.searchParams.get("error")) return failure("access_denied");
  const code = request.nextUrl.searchParams.get("code") || "";
  if (!code) return failure("missing_code");
  try {
    const redirectUri = `${origin}/api/meta/callback`;
    const credentials = await exchangeMetaCode(code, redirectUri);
    const encrypted = encryptMetaCredentials(credentials);
    await saveEncryptedMetaCredentials(JSON.stringify(encrypted));
    return clearState(NextResponse.redirect(`${origin}/settings?meta=connected`));
  } catch {
    return failure("connection_failed");
  }
}
