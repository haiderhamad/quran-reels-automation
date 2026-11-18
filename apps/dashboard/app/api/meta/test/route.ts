import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { getEncryptedMetaCredentials } from "@/lib/b2";
import { testMetaConnection } from "@/lib/meta-api";
import { decryptMetaCredentials, type EncryptedMetaCredentials } from "@/lib/meta-credentials";

export async function POST() {
  if (!(await getAdminSession())) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  try {
    const raw = await getEncryptedMetaCredentials();
    if (!raw) return NextResponse.json({ error: "Meta is not connected." }, { status: 409 });
    const result = await testMetaConnection(
      decryptMetaCredentials(JSON.parse(raw) as EncryptedMetaCredentials)
    );
    return NextResponse.json(result, { status: result.facebook ? 200 : 502 });
  } catch {
    return NextResponse.json({ error: "Meta connection test failed." }, { status: 502 });
  }
}
