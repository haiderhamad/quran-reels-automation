import { NextRequest, NextResponse } from "next/server";
import { getAdminSession } from "@/lib/auth";
import { checkMediaExists, createPreviewPresignedUrl } from "@/lib/b2";

export async function POST(request: NextRequest) {
  const isAuth = await getAdminSession();
  if (!isAuth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const body = await request.json();
    const { mediaKey } = body;

    if (!mediaKey || typeof mediaKey !== "string") {
      return NextResponse.json(
        { error: "mediaKey is required." },
        { status: 400 }
      );
    }

    const exists = await checkMediaExists(mediaKey);
    if (!exists) {
      return NextResponse.json({
        exists: false,
        message: "Media expired or not found under 7-day retention policy.",
      });
    }

    // 10-minute preview presigned URL
    const previewUrl = await createPreviewPresignedUrl(mediaKey, 600);
    return NextResponse.json({
      exists: true,
      previewUrl,
    });
  } catch (err: any) {
    console.error("[API_PREVIEW] Error:", err);
    return NextResponse.json(
      { error: err.message || "Failed to generate preview URL." },
      { status: 500 }
    );
  }
}
