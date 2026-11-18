import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { triggerBackgroundDiscovery, triggerWorkflowDispatch } from "@/lib/github";
import { getStoredConfig } from "@/lib/b2";
import { getPublishPlatformBlockReason, getSystemConnectionStatusWithPublishing, isPublishNowAllowed } from "@/lib/system";
import { randomUUID } from "crypto";
import {
  CALM_REELS_V1_MANUAL_PUBLISH_READY,
  CALM_REELS_V1_MANUAL_PUBLISH_BLOCK_REASON,
} from "@/lib/calm-reels-shortlist";

export async function POST(request: NextRequest) {
  const isAuth = await getAdminSession();
  if (!isAuth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const body = await request.json();
    const action = body.action || (body.run_kind === "manual_production" ? "publish_now" : "generate_test");
    const runKind: "test" | "manual_production" | undefined = action === "publish_now" ? "manual_production" : action === "generate_test" ? "test" : undefined;
    const testVerseKey = body.test_verse_key === "" || body.test_verse_key == null
      ? undefined
      : body.test_verse_key;

    if (action === "background_discovery") {
      const result = await triggerBackgroundDiscovery();
      return result.success
        ? NextResponse.json(result)
        : NextResponse.json({ error: result.message }, { status: 500 });
    }
    if (action !== "generate_test" && action !== "publish_now") {
      return NextResponse.json(
        { error: "Unsupported operations action." },
        { status: 400 }
      );
    }
    if (!runKind) {
      return NextResponse.json({ error: "Unsupported operations action." }, { status: 400 });
    }

    if (testVerseKey !== undefined && (runKind !== "test" || testVerseKey !== "2:21")) {
      return NextResponse.json(
        { error: "test_verse_key must be the approved 2:21 fixture on a test run." },
        { status: 400 }
      );
    }

    if (testVerseKey === "2:21") {
      const config = await getStoredConfig();
      if (
        config.translation_languages.length !== 1
        || config.translation_languages[0] !== "en"
      ) {
        return NextResponse.json(
          { error: "The verified 2:21 phrase example requires English-only translation display." },
          { status: 409 }
        );
      }
    }

    const publishFacebook = body.publish_facebook === true;
    const publishInstagram = body.publish_instagram === true;
    if (runKind === "manual_production") {
      if (CALM_REELS_V1_MANUAL_PUBLISH_READY && (!publishFacebook || !publishInstagram)) {
        return NextResponse.json(
          { error: "Owner-v1 publishing requires both Facebook and Instagram Buffer channels." },
          { status: 400 },
        );
      }
      if (!publishFacebook && !publishInstagram) {
        return NextResponse.json({ error: "Select Facebook, Instagram, or both publishing targets." }, { status: 400 });
      }
      const [config, systemStatus] = await Promise.all([
        getStoredConfig(),
        getSystemConnectionStatusWithPublishing(),
      ]);
      if (!isPublishNowAllowed(config.reciter_reuse_confirmed, systemStatus.reciterReuseRuntimeGate)) {
        return NextResponse.json(
          { error: "PUBLICATION_BLOCKED_RECITER_RIGHTS", code: "PUBLICATION_BLOCKED_RECITER_RIGHTS" },
          { status: 403 }
        );
      }
      const platformBlockReason = getPublishPlatformBlockReason(
        publishFacebook && config.facebook_enabled,
        publishInstagram && config.instagram_enabled,
        systemStatus.bufferFacebook,
        systemStatus.bufferInstagram,
      );
      if (platformBlockReason) {
        return NextResponse.json(
          { error: `Publish Now is blocked: ${platformBlockReason}` },
          { status: 409 }
        );
      }
    }

    // Dashboard readiness and transport credentials are not proof that the
    // frozen owner-v1 cycle can be safely posted. Reject before GitHub dispatch.
    if (runKind === "manual_production" && !CALM_REELS_V1_MANUAL_PUBLISH_READY) {
      return NextResponse.json(
        { error: CALM_REELS_V1_MANUAL_PUBLISH_BLOCK_REASON, code: "OWNER_V1_PUBLISH_NOT_READY" },
        { status: 409 },
      );
    }

    const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
    const runId = `${runKind === "test" ? "test" : "manual"}_${stamp}_${randomUUID().slice(0, 8)}`;
    const result = await triggerWorkflowDispatch({ runKind, runId, testVerseKey, publishFacebook, publishInstagram });
    if (!result.success) {
      return NextResponse.json({ error: result.message }, { status: 500 });
    }

    return NextResponse.json({ ...result, runId, runKind, action });
  } catch (err: any) {
    console.error("[API_TRIGGER] Error:", err);
    return NextResponse.json(
      { error: err.message || "Failed to trigger workflow dispatch." },
      { status: 500 }
    );
  }
}
