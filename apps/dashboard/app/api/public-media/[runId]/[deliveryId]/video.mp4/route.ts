import { GetObjectCommand, HeadObjectCommand } from "@aws-sdk/client-s3";
import { getB2Client, getB2ConnectionConfig, getRunRecordById } from "@/lib/b2";
import { canServePublicMedia } from "@/lib/public-media";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function notFound(): Response {
  // Deliberately do not reveal whether a RunRecord, delivery ID, or B2 object
  // exists.  This endpoint can only ever serve one final-safe MP4.
  return new Response("Not found", { status: 404, headers: { "Cache-Control": "no-store" } });
}

async function resolveAuthorizedMedia(
  context: { params: Promise<{ runId: string; deliveryId: string }> },
): Promise<{ mediaKey: string; b2: ReturnType<typeof getB2ConnectionConfig> } | null> {
  const { runId, deliveryId } = await context.params;
  const record = await getRunRecordById(runId);
  const b2 = getB2ConnectionConfig();
  if (!record) {
    console.error(`[PublicMedia] RunRecord not found for runId: ${runId}`);
    return null;
  }
  if (!canServePublicMedia(record, runId, deliveryId, b2.prefix)) {
    console.error(`[PublicMedia] canServePublicMedia returned false for runId: ${runId}, deliveryId: ${deliveryId}`);
    return null;
  }
  if (!record.media_key) {
    console.error(`[PublicMedia] record.media_key missing for runId: ${runId}`);
    return null;
  }
  return { mediaKey: record.media_key, b2 };
}

function mediaHeaders(contentLength?: number, eTag?: string): Headers {
  const headers = new Headers({
    "Content-Type": "video/mp4",
    // A delivery ID is revocable state.  Do not allow Vercel/browser CDN
    // caches to keep serving a capability after its RunRecord is invalidated.
    "Cache-Control": "public, max-age=0, s-maxage=0, must-revalidate",
    "X-Content-Type-Options": "nosniff",
  });
  if (contentLength != null) headers.set("Content-Length", String(contentLength));
  if (eTag) headers.set("ETag", eTag);
  return headers;
}

export async function GET(
  _request: Request,
  context: { params: Promise<{ runId: string; deliveryId: string }> },
): Promise<Response> {
  const resolved = await resolveAuthorizedMedia(context);
  if (!resolved) return notFound();

  try {
    const object = await getB2Client().send(new GetObjectCommand({
      Bucket: resolved.b2.bucketName,
      Key: resolved.mediaKey,
    }));
    if (!object.Body || typeof object.Body.transformToWebStream !== "function") return notFound();
    return new Response(
      object.Body.transformToWebStream(),
      { status: 200, headers: mediaHeaders(object.ContentLength, object.ETag) },
    );
  } catch (err) {
    console.error(`[PublicMedia] Error streaming object from B2 for key ${resolved.mediaKey}:`, err);
    return notFound();
  }
}

/** Buffer and other video ingesters may validate a direct endpoint with HEAD. */
export async function HEAD(
  _request: Request,
  context: { params: Promise<{ runId: string; deliveryId: string }> },
): Promise<Response> {
  const resolved = await resolveAuthorizedMedia(context);
  if (!resolved) return notFound();
  try {
    const object = await getB2Client().send(new HeadObjectCommand({
      Bucket: resolved.b2.bucketName,
      Key: resolved.mediaKey,
    }));
    return new Response(null, { status: 200, headers: mediaHeaders(object.ContentLength, object.ETag) });
  } catch (err) {
    console.error(`[PublicMedia] Error heading object from B2 for key ${resolved.mediaKey}:`, err);
    return notFound();
  }
}
