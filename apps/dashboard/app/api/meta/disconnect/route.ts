import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { deleteEncryptedMetaCredentials } from "@/lib/b2";

export async function POST() {
  if (!(await getAdminSession())) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  try {
    await deleteEncryptedMetaCredentials();
    return NextResponse.json({ success: true, connected: false });
  } catch {
    return NextResponse.json({ error: "Meta disconnect failed." }, { status: 500 });
  }
}
