import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { getStoredConfigState, saveStoredConfig } from "@/lib/b2";
import { runSchedulerTick, type SchedulerTickResult } from "@/lib/scheduler-server";
import { StoredConfigSchema } from "@/lib/schema";
import { assertProductionPublishingSchedule } from "@/lib/schedule";

export async function GET() {
  const isAuth = await getAdminSession();
  if (!isAuth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const state = await getStoredConfigState();
    return NextResponse.json({
      config: state.config,
      authoritative: state.authoritative,
      writable: state.writable,
      errorCode: state.errorCode,
    }, { status: state.authoritative ? 200 : 503 });
  } catch (err: any) {
    console.error("[API_SETTINGS_GET] Error:", err);
    return NextResponse.json(
      { error: err.message || "Failed to load configuration from B2." },
      { status: 500 }
    );
  }
}

export async function POST(request: NextRequest) {
  const isAuth = await getAdminSession();
  if (!isAuth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const body = await request.json();

    // Validate with Zod
    const validated = StoredConfigSchema.parse(body);
    assertProductionPublishingSchedule(validated.publishing_schedule);

    const saved = await saveStoredConfig(validated);

    let schedulerResult: SchedulerTickResult | null = null;
    try {
      schedulerResult = await runSchedulerTick(new Date(), saved, "dashboard_save");
    } catch (schedErr: any) {
      console.error("[API_SETTINGS_POST] Immediate scheduler evaluation failed closed:", schedErr);
    }

    return NextResponse.json({
      success: true,
      config: saved,
      scheduler: schedulerResult,
    });
  } catch (err: any) {
    console.error("[API_SETTINGS_POST] Error:", err);
    const unavailable = err?.message === "B2_CONFIG_UNAVAILABLE_READ_ONLY";
    return NextResponse.json(
      { error: err.message || "Failed to save configuration to B2." },
      { status: unavailable ? 503 : 400 }
    );
  }
}
