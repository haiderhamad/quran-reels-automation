import "server-only";

export interface WorkflowDispatchResult {
  success: boolean;
  message: string;
  workflowUrl?: string;
  ambiguous?: boolean;
}

export interface WorkflowDispatchRequest {
  runKind: "test" | "manual_production" | "scheduled";
  runId: string;
  slotId?: string;
  force?: boolean;
  testVerseKey?: "2:21";
  publishFacebook?: boolean;
  publishInstagram?: boolean;
}

export interface WorkflowDispatchPayload {
  ref: string;
  inputs: {
    run_kind: WorkflowDispatchRequest["runKind"];
    schedule_slot_id: string;
    run_id: string;
    force: boolean;
    test_verse_key: string;
    publish_facebook: boolean;
    publish_instagram: boolean;
  };
}

const DISPATCH_ID_SAFE = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}$/;

export function buildWorkflowDispatchPayload(
  request: WorkflowDispatchRequest,
  ref: string
): WorkflowDispatchPayload {
  if (!DISPATCH_ID_SAFE.test(request.runId)) {
    throw new Error("Workflow run ID must be a path-safe non-empty identifier.");
  }
  const slotId = request.slotId || "";
  if (request.runKind === "scheduled" && !DISPATCH_ID_SAFE.test(slotId)) {
    throw new Error("Scheduled workflow dispatch requires a path-safe schedule slot ID.");
  }
  if (request.runKind !== "scheduled" && slotId) {
    throw new Error("Only scheduled workflow dispatches may include a schedule slot ID.");
  }
  if (request.testVerseKey && request.runKind !== "test") {
    throw new Error("Only test workflow dispatches may include a test verse key.");
  }
  if (request.testVerseKey && request.testVerseKey !== "2:21") {
    throw new Error("Unsupported verified test verse key.");
  }
  return {
    ref,
    inputs: {
      run_kind: request.runKind,
      schedule_slot_id: slotId,
      run_id: request.runId,
      force: Boolean(request.force),
      test_verse_key: request.testVerseKey || "",
      publish_facebook: request.publishFacebook !== false,
      publish_instagram: request.publishInstagram !== false,
    },
  };
}

export async function triggerWorkflowDispatch(
  request: WorkflowDispatchRequest | "morning" | "evening" | "test",
  force = false
): Promise<WorkflowDispatchResult> {
  const isLegacy = typeof request === "string";
  const legacySlot = isLegacy ? request : null;
  const runKind = isLegacy
    ? request === "test" ? "test" : "scheduled"
    : request.runKind;
  const slotId = isLegacy
    ? request === "test" ? "" : request
    : request.slotId || "";
  const runId = isLegacy
    ? `legacy_${request}_${Date.now()}`
    : request.runId;
  const effectiveForce = isLegacy ? force : Boolean(request.force);
  const owner = process.env.GITHUB_OWNER?.trim();
  const repo = process.env.GITHUB_REPO?.trim();
  const workflowId = process.env.GITHUB_WORKFLOW_ID?.trim() || "generate-reel.yml";
  const ref = process.env.GITHUB_WORKFLOW_REF?.trim() || "main";
  const token = process.env.GITHUB_ACTIONS_TOKEN?.trim();

  if (!owner || !repo || !token) {
    return {
      success: false,
      message:
        "GitHub Actions dispatch is not configured. Missing GITHUB_OWNER, GITHUB_REPO, or GITHUB_ACTIONS_TOKEN in environment.",
    };
  }

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowId}/dispatches`;

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "QuranReelsDashboard/1.0",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(buildWorkflowDispatchPayload({
        runKind,
        slotId: slotId || undefined,
        runId,
        force: effectiveForce,
        testVerseKey: isLegacy ? undefined : request.testVerseKey,
        publishFacebook: isLegacy ? undefined : request.publishFacebook,
        publishInstagram: isLegacy ? undefined : request.publishInstagram,
      }, ref)),
    });

    if (response.status === 204) {
      const actionsUrl = `https://github.com/${owner}/${repo}/actions/workflows/${workflowId}`;
      return {
        success: true,
        message: legacySlot
          ? `Generation requested successfully for '${legacySlot}' slot (force=${effectiveForce}). GitHub Actions workflow triggered.`
          : `GitHub Actions accepted the ${runKind.replace("_", " ")} request.`,
        workflowUrl: actionsUrl,
      };
    }

    const errorBody = await response.text();
    let parsedMessage = errorBody;
    try {
      const json = JSON.parse(errorBody);
      parsedMessage = json.message || errorBody;
    } catch {
      // ignore
    }

    return {
      success: false,
      message: `GitHub API error (${response.status}): ${parsedMessage}`,
    };
  } catch (err: any) {
    console.error("[GITHUB] Failed to trigger workflow dispatch:", err);
    return {
      success: false,
      message: `Network error triggering GitHub Actions: ${err.message || err}`,
      ambiguous: true,
    };
  }
}

/** Dispatch only the fixed metadata-discovery workflow; no browser-controlled workflow IDs. */
export async function triggerBackgroundDiscovery(): Promise<WorkflowDispatchResult> {
  const owner = process.env.GITHUB_OWNER?.trim();
  const repo = process.env.GITHUB_REPO?.trim();
  const token = process.env.GITHUB_ACTIONS_TOKEN?.trim();
  const ref = process.env.GITHUB_WORKFLOW_REF?.trim() || "main";
  if (!owner || !repo || !token) {
    return { success: false, message: "GitHub Actions dispatch is not configured." };
  }
  try {
    const response = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/actions/workflows/background-discovery.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "QuranReelsDashboard/1.0",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref, inputs: { max_candidates: "24" } }),
      },
    );
    if (response.status === 204) {
      return { success: true, message: "GitHub Actions accepted the background discovery request.", workflowUrl: `https://github.com/${owner}/${repo}/actions/workflows/background-discovery.yml` };
    }
    return { success: false, message: `GitHub API error (${response.status}): ${await response.text()}` };
  } catch (err: any) {
    console.error("[GITHUB] Failed to trigger background discovery:", err);
    return { success: false, message: "Network error triggering GitHub Actions.", ambiguous: true };
  }
}
