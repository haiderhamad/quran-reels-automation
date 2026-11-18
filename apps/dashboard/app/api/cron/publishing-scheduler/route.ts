import { NextRequest, NextResponse } from "next/server";
import { getStoredConfig } from "@/lib/b2";
import { getBerlinClock, getDuePublishingSlots } from "@/lib/schedule";
import { runSchedulerTick } from "@/lib/scheduler-server";

export async function GET(request: NextRequest) {
  // 1. Authenticate using Vercel CRON_SECRET Bearer token
  const authHeader = request.headers.get("authorization");
  const cronSecret = process.env.CRON_SECRET?.trim();

  if (!cronSecret || authHeader !== `Bearer ${cronSecret}`) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const now = new Date();
    const config = await getStoredConfig();
    const clock = getBerlinClock(now);
    const dueSlots = config.automation_enabled
      ? getDuePublishingSlots(config.publishing_schedule, now)
      : [];

    const userAgent = (request.headers.get("user-agent") || "").toLowerCase();
    const source =
      userAgent.includes("vercel")
        ? "vercel_cron"
        : userAgent.includes("github")
        ? "github_cron"
        : "supabase_cron";

    const result = await runSchedulerTick(now, config, source);

    let noOpReason: string | null = null;
    if (!result.automationEnabled) {
      noOpReason = "Automation is disabled in configuration.";
    } else if (dueSlots.length === 0) {
      noOpReason = "No publishing slots are currently due within the grace window.";
    } else if (result.dispatched.length === 0 && result.skipped.length > 0) {
      noOpReason = "All due slots were already claimed or have existing run records.";
    }

    return NextResponse.json({
      success: true,
      timestamp: now.toISOString(),
      berlinClock: clock,
      automationEnabled: result.automationEnabled,
      dueSlots: dueSlots.map((s) => s.id),
      claimsAcquired: result.dispatched,
      dispatchesTriggered: result.dispatched,
      skippedSlots: result.skipped,
      failedSlots: result.failed,
      noOpReason,
    });
  } catch (err: any) {
    console.error("[CRON_PUBLISHING_SCHEDULER] Error during execution:", err.message || err);
    return NextResponse.json(
      {
        success: false,
        error: "Scheduler execution failed closed.",
      },
      { status: 500 }
    );
  }
}
