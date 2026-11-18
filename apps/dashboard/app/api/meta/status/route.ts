import { NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { getEncryptedMetaCredentials } from "@/lib/b2";
import {
  decryptMetaCredentials,
  type EncryptedMetaCredentials,
  safeMetaCredentialStatus,
} from "@/lib/meta-credentials";

export async function GET() {
  if (!(await getAdminSession())) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  try {
    const raw = await getEncryptedMetaCredentials();
    if (!raw) return NextResponse.json({ connected: false });
    const credentials = decryptMetaCredentials(JSON.parse(raw) as EncryptedMetaCredentials);
    return NextResponse.json(safeMetaCredentialStatus(credentials));
  } catch {
    return NextResponse.json({ connected: false, error: "Meta credentials could not be verified." }, { status: 503 });
  }
}
