import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { findRunRecordById } from "@/lib/b2";

export async function GET(request: NextRequest) {
  if (!(await getAdminSession())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const runId = request.nextUrl.searchParams.get("runId")?.trim() || "";
  if (!/^[a-zA-Z0-9_.-]{1,160}$/.test(runId)) {
    return NextResponse.json({ error: "A valid runId is required." }, { status: 400 });
  }
  try {
    const run = await findRunRecordById(runId);
    return NextResponse.json({ found: Boolean(run), run });
  } catch (err: any) {
    console.error("[API_ACTION_STATUS] Failed to load run status:", err);
    return NextResponse.json({ error: "Could not load run status from Backblaze B2." }, { status: 500 });
  }
}
