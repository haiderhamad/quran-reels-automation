import { getStoredConfigState } from "@/lib/b2";
import { getSystemConnectionStatusWithPublishing } from "@/lib/system";
import { SettingsForm } from "@/components/SettingsForm";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const configState = await getStoredConfigState();
  const sysStatus = await getSystemConnectionStatusWithPublishing();

  return (
    <div className="settings-workspace-page" style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
      <div>
        <h1 style={{ fontSize: "20px", fontWeight: 600 }}>Settings Control Center</h1>
        <p style={{ color: "var(--text-muted)", fontSize: "13px", marginTop: "2px" }}>
          Manage future Reel text, languages, schedule, generation, and publishing readiness.
        </p>
      </div>

      <SettingsForm
        initialConfig={configState.config}
        systemStatus={sysStatus}
        configWritable={configState.writable}
      />
    </div>
  );
}
