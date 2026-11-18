import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { buildWorkflowDispatchPayload, type WorkflowDispatchRequest } from "../lib/github";

const workflowPath = resolve(process.cwd(), "../../.github/workflows/generate-reel.yml");

function parseWorkflowDispatchInputNames(workflow: string): string[] {
  const dispatchStart = workflow.indexOf("  workflow_dispatch:\n");
  if (dispatchStart < 0) throw new Error("workflow_dispatch block is missing");
  const permissionsStart = workflow.indexOf("\npermissions:", dispatchStart);
  if (permissionsStart < 0) throw new Error("permissions block after workflow_dispatch is missing");
  const block = workflow.slice(dispatchStart, permissionsStart);
  return [...block.matchAll(/^      ([a-z][a-z0-9_]*):\s*$/gm)].map((match) => match[1]);
}

describe("Dashboard to GitHub workflow_dispatch contract", () => {
  const workflow = readFileSync(workflowPath, "utf-8");
  const declaredInputs = parseWorkflowDispatchInputNames(workflow);

  it("declares exactly the input keys sent by the Dashboard", () => {
    const payload = buildWorkflowDispatchPayload({
      runKind: "scheduled",
      slotId: "stable-slot",
      runId: "scheduled_20260824_stable-slot",
    }, "main");
    expect(Object.keys(payload.inputs).sort()).toEqual([...declaredInputs].sort());
    expect(declaredInputs).toEqual(["run_kind", "schedule_slot_id", "run_id", "force", "test_verse_key", "publish_facebook", "publish_instagram"]);
  });

  it.each<WorkflowDispatchRequest>([
    { runKind: "test", runId: "test_20260824_1" },
    { runKind: "manual_production", runId: "manual_20260824_1" },
    { runKind: "scheduled", slotId: "morning", runId: "scheduled_20260824_morning" },
    { runKind: "scheduled", slotId: "stable-a", runId: "scheduled_20260824_stable-a" },
  ])("maps $runKind requests to workflow-supported engine inputs", (request) => {
    const payload = buildWorkflowDispatchPayload(request, "main");
    expect(workflow).toContain(`- \"${payload.inputs.run_kind}\"`);
    expect(workflow).toContain("JOB_RUN_KIND: ${{ inputs.run_kind }}");
    expect(workflow).toContain("JOB_SCHEDULE_SLOT_ID: ${{ inputs.schedule_slot_id }}");
    expect(workflow).toContain("JOB_REQUEST_RUN_ID: ${{ inputs.run_id }}");
    expect(workflow).toContain("JOB_TEST_VERSE_KEY: ${{ inputs.test_verse_key }}");
    expect(workflow).toMatch(/python3 (?:-u )?-m engine\.job "\$\{job_args\[@\]\}"/);
  });

  it("passes the approved phrase fixture only on test dispatches", () => {
    const payload = buildWorkflowDispatchPayload({
      runKind: "test",
      runId: "test_2_21",
      testVerseKey: "2:21",
    }, "main");
    expect(payload.inputs.test_verse_key).toBe("2:21");
    expect(() => buildWorkflowDispatchPayload({
      runKind: "manual_production",
      runId: "manual_no_override",
      testVerseKey: "2:21",
    }, "main")).toThrow("Only test");
  });
});
