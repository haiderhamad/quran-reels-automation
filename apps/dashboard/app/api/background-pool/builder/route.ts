import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { getStoredConfigState } from "@/lib/b2";
import {
  LOCAL_POOL_ALLOWED_PROVIDERS,
  LOCAL_POOL_ALLOWED_TARGETS,
  localBuilderRequestAllowed,
  readLocalBuilderStatus,
  refreshLocalPoolTotals,
  startLocalBuilder,
  stopLocalBuilder,
  type LocalPoolProvider,
} from "@/lib/local-background-pool-builder";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function response(body: unknown, status = 200) {
  const result = NextResponse.json(body, { status });
  result.headers.set("Cache-Control", "no-store");
  return result;
}

function authorityHostnames(value: string | null): string[] | null {
  if (!value) return [];
  try {
    return value.split(",").map((part) => new URL(`http://${part.trim()}`).hostname);
  } catch {
    return null;
  }
}

function originHostname(value: string | null): string[] | null {
  if (!value) return [];
  try {
    return [new URL(value).hostname];
  } catch {
    return null;
  }
}

function denied(request: NextRequest): boolean {
  const hostnames = [
    authorityHostnames(request.headers.get("host")),
    authorityHostnames(request.headers.get("x-forwarded-host")),
    originHostname(request.headers.get("origin")),
  ];
  if (hostnames.some((items) => items === null)) return true;
  return !localBuilderRequestAllowed(
    request.nextUrl.hostname,
    process.env,
    hostnames.flatMap((items) => items || []),
  );
}

function sanitizedActionError(error: unknown) {
  const message = error instanceof Error ? error.message : "";
  if (message === "LOCAL_POOL_BUILDER_ALREADY_RUNNING") {
    return response({ error: message }, 409);
  }
  if (message === "LOCAL_POOL_REFRESH_WHILE_RUNNING") {
    return response({ error: message }, 409);
  }
  if (message === "B2_CONFIG_UNAVAILABLE_READ_ONLY") {
    return response({ error: message }, 503);
  }
  if (message === "LOCAL_POOL_INVALID_TARGET" || message === "LOCAL_POOL_INVALID_PROVIDER") {
    return response({ error: message }, 400);
  }
  if (/^LOCAL_POOL_CONFIG_MISSING:(?:[A-Z0-9_]+,?)+$/.test(message)) {
    return response({ error: message }, 400);
  }
  return response({ error: "LOCAL_POOL_ACTION_FAILED" }, 500);
}

export async function GET(request: NextRequest) {
  if (denied(request)) return response({ error: "Not found" }, 404);
  if (!(await getAdminSession())) return response({ error: "Unauthorized" }, 401);
  return response({ status: readLocalBuilderStatus() });
}

export async function POST(request: NextRequest) {
  if (denied(request)) return response({ error: "Not found" }, 404);
  if (!(await getAdminSession())) return response({ error: "Unauthorized" }, 401);
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return response({ error: "LOCAL_POOL_INVALID_REQUEST" }, 400);
  }
  if (!body || typeof body !== "object") {
    return response({ error: "LOCAL_POOL_INVALID_REQUEST" }, 400);
  }
  const input = body as Record<string, unknown>;
  try {
    if (input.action === "start") {
      if (
        typeof input.addSafe !== "number"
        || !LOCAL_POOL_ALLOWED_TARGETS.includes(input.addSafe as typeof LOCAL_POOL_ALLOWED_TARGETS[number])
        || !LOCAL_POOL_ALLOWED_PROVIDERS.includes(input.provider as LocalPoolProvider)
      ) {
        return response({ error: "LOCAL_POOL_INVALID_REQUEST" }, 400);
      }
      const configState = await getStoredConfigState();
      return response({
        status: startLocalBuilder(
          input.addSafe,
          input.provider as LocalPoolProvider,
          process.env,
          undefined,
          configState.authoritative,
        ),
      });
    }
    if (input.action === "stop") {
      return response({ status: stopLocalBuilder() });
    }
    if (input.action === "refresh_totals") {
      return response({ status: await refreshLocalPoolTotals() });
    }
    return response({ error: "LOCAL_POOL_INVALID_REQUEST" }, 400);
  } catch (error) {
    return sanitizedActionError(error);
  }
}
