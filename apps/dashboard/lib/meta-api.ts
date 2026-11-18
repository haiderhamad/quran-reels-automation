import "server-only";

import type { MetaCredentialPayload } from "./meta-credentials";

export const META_REQUIRED_SCOPES = [
  "pages_show_list",
  "pages_read_engagement",
  "pages_manage_posts",
  "instagram_basic",
  "instagram_content_publish",
] as const;

interface TokenResponse {
  access_token: string;
  token_type?: string;
  expires_in?: number;
}

interface PageRecord {
  id: string;
  name: string;
  access_token: string;
  instagram_business_account?: { id: string; username?: string; name?: string } | null;
}

function graphVersion(): string {
  const value = process.env.META_API_VERSION?.trim() || "v26.0";
  if (!/^v\d+(?:\.\d+)?$/.test(value)) throw new Error("Invalid META_API_VERSION.");
  return value;
}

function appConfig(): { appId: string; appSecret: string } {
  const appId = process.env.META_APP_ID?.trim() || "";
  const appSecret = process.env.META_APP_SECRET?.trim() || "";
  if (!appId || !appSecret) throw new Error("Meta OAuth app credentials are not configured.");
  return { appId, appSecret };
}

export function resolveMetaRedirectOrigin(requestUrl: string): string {
  const configured = process.env.META_REDIRECT_ORIGIN?.trim();
  if (configured) {
    const configuredUrl = new URL(configured);
    if (configuredUrl.protocol !== "https:") {
      throw new Error("META_REDIRECT_ORIGIN must use HTTPS.");
    }
    return configuredUrl.origin;
  }
  const origin = new URL(requestUrl).origin;
  const parsed = new URL(origin);
  const isLocal = parsed.protocol === "http:" && ["localhost", "127.0.0.1"].includes(parsed.hostname);
  if (isLocal) return origin;
  const vercelHosts = [
    process.env.VERCEL_URL,
    process.env.VERCEL_BRANCH_URL,
    process.env.VERCEL_PROJECT_PRODUCTION_URL,
  ]
    .map((value) => value?.trim().replace(/^https?:\/\//, ""))
    .filter(Boolean);
  if (parsed.protocol === "https:" && vercelHosts.includes(parsed.host)) return origin;
  throw new Error("Configure META_REDIRECT_ORIGIN for non-local, non-Vercel deployments.");
}

export function buildMetaOAuthUrl(redirectUri: string, state: string): string {
  const { appId } = appConfig();
  const url = new URL(`https://www.facebook.com/${graphVersion()}/dialog/oauth`);
  url.searchParams.set("client_id", appId);
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("state", state);
  url.searchParams.set("response_type", "code");
  const loginConfigId = process.env.META_LOGIN_CONFIG_ID?.trim();
  if (loginConfigId) {
    // Facebook Login for Business configurations own the permission set;
    // Meta recommends config_id instead of the legacy scope parameter.
    url.searchParams.set("config_id", loginConfigId);
    url.searchParams.set("override_default_response_type", "true");
  } else {
    url.searchParams.set("scope", META_REQUIRED_SCOPES.join(","));
  }
  return url.toString();
}

async function requestJson<T>(
  url: URL,
  fetchFn: typeof fetch,
  authorizationToken?: string
): Promise<T> {
  const response = await fetchFn(url, {
    headers: {
      Accept: "application/json",
      ...(authorizationToken ? { Authorization: `Bearer ${authorizationToken}` } : {}),
    },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Meta Graph API request failed with HTTP ${response.status}.`);
  return (await response.json()) as T;
}

async function requestToken(
  version: string,
  parameters: Record<string, string>,
  fetchFn: typeof fetch,
): Promise<TokenResponse> {
  const response = await fetchFn(`https://graph.facebook.com/${version}/oauth/access_token`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams(parameters),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Meta OAuth token exchange failed with HTTP ${response.status}.`);
  return await response.json() as TokenResponse;
}

export async function exchangeMetaCode(
  code: string,
  redirectUri: string,
  fetchFn: typeof fetch = fetch
): Promise<MetaCredentialPayload> {
  const { appId, appSecret } = appConfig();
  const version = graphVersion();
  const shortLived = await requestToken(version, {
    client_id: appId,
    client_secret: appSecret,
    redirect_uri: redirectUri,
    code,
  }, fetchFn);
  if (!shortLived.access_token) throw new Error("Meta did not return a user access token.");

  const longLived = await requestToken(version, {
    grant_type: "fb_exchange_token",
    client_id: appId,
    client_secret: appSecret,
    fb_exchange_token: shortLived.access_token,
  }, fetchFn);
  if (!longLived.access_token) throw new Error("Meta did not return a long-lived user access token.");

  const pagesUrl = new URL(`https://graph.facebook.com/${version}/me/accounts`);
  pagesUrl.searchParams.set(
    "fields",
    "id,name,access_token,instagram_business_account{id,username,name}"
  );
  const pages = await requestJson<{ data?: PageRecord[] }>(pagesUrl, fetchFn, longLived.access_token);
  const candidates = (pages.data || []).filter((page) => page.id && page.name && page.access_token);
  const preferredId = process.env.META_TARGET_PAGE_ID?.trim() || process.env.META_PAGE_ID?.trim();
  const selected = preferredId
    ? candidates.find((page) => page.id === preferredId)
    : candidates.length === 1
      ? candidates[0]
      : undefined;
  if (!selected) {
    throw new Error(
      candidates.length > 1
        ? "Multiple Facebook Pages are available; configure META_TARGET_PAGE_ID."
        : "No eligible Facebook Page was returned by Meta."
    );
  }

  const meUrl = new URL(`https://graph.facebook.com/${version}/me`);
  meUrl.searchParams.set("fields", "id");
  const user = await requestJson<{ id?: string }>(meUrl, fetchFn, longLived.access_token);
  if (!user.id) throw new Error("Meta did not return the connected user identity.");

  const permissionsUrl = new URL(`https://graph.facebook.com/${version}/me/permissions`);
  const permissions = await requestJson<{
    data?: Array<{ permission?: string; status?: string }>;
  }>(permissionsUrl, fetchFn, longLived.access_token);
  const grantedScopes = (permissions.data || [])
    .filter((entry) => entry.status === "granted" && entry.permission)
    .map((entry) => entry.permission as string);
  const requiredFacebookScopes = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"];
  if (requiredFacebookScopes.some((scope) => !grantedScopes.includes(scope))) {
    throw new Error("Meta did not grant all required Facebook Page permissions.");
  }
  const instagramGranted = ["instagram_basic", "instagram_content_publish"].every((scope) =>
    grantedScopes.includes(scope)
  );

  const now = new Date();
  return {
    schema_version: 1,
    connected_at: now.toISOString(),
    graph_api_version: version,
    user_id: user.id,
    page_id: selected.id,
    page_name: selected.name,
    page_access_token: selected.access_token,
    scopes: grantedScopes,
    // The Page token derived from a long-lived user token has no scheduled
    // expiry per Meta; it can still be invalidated, so `/api/meta/test` probes it.
    token_expires_at: null,
    instagram: selected.instagram_business_account && instagramGranted
      ? {
          id: selected.instagram_business_account.id,
          username: selected.instagram_business_account.username || null,
          name: selected.instagram_business_account.name || null,
        }
      : null,
  };
}

export async function testMetaConnection(
  credentials: MetaCredentialPayload,
  fetchFn: typeof fetch = fetch
): Promise<{ facebook: boolean; instagram: boolean }> {
  const url = new URL(
    `https://graph.facebook.com/${credentials.graph_api_version}/${encodeURIComponent(credentials.page_id)}`
  );
  url.searchParams.set("fields", "id,instagram_business_account{id}");
  const response = await fetchFn(url, {
    headers: { Authorization: `Bearer ${credentials.page_access_token}`, Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) return { facebook: false, instagram: false };
  const data = (await response.json()) as { id?: string; instagram_business_account?: { id?: string } };
  const facebook = data.id === credentials.page_id;
  const instagram = Boolean(
    facebook &&
    credentials.instagram?.id &&
    data.instagram_business_account?.id === credentials.instagram.id
  );
  return { facebook, instagram };
}
