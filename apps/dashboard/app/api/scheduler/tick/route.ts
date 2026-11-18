import { timingSafeEqual } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { runSchedulerTick } from "@/lib/scheduler-server";

function secretMatches(provided: string, expected: string): boolean {
  const left = Buffer.from(provided, "utf-8");
  const right = Buffer.from(expected, "utf-8");
  return left.length === right.length && timingSafeEqual(left, right);
}

export async function POST(request: NextRequest) {
  const expected = process.env.SCHEDULER_SECRET?.trim() || "";
  if (!expected) {
    return NextResponse.json({ error: "Scheduler is not configured." }, { status: 503 });
  }
  const authorization = request.headers.get("authorization") || "";
  const provided = authorization.startsWith("Bearer ") ? authorization.slice(7).trim() : "";
  if (!provided || !secretMatches(provided, expected)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  try {
    return NextResponse.json(await runSchedulerTick());
  } catch (err: any) {
    console.error("[SCHEDULER_TICK] Failed:", err);
    return NextResponse.json({ error: "Scheduler tick failed closed." }, { status: 500 });
  }
}
