import { requireAdminSession } from "@/lib/auth";
import { getStoredConfig } from "@/lib/b2";
import { Navbar } from "@/components/Navbar";

export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await requireAdminSession();

  let automationEnabled = false;
  try {
    const config = await getStoredConfig();
    automationEnabled = config.automation_enabled;
  } catch {
    // If R2 is not reachable yet, default to false
  }

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <Navbar automationEnabled={automationEnabled} />
      <main style={{ flex: 1, padding: "24px 0" }}>
        <div className="app-container">{children}</div>
      </main>
      <footer
        style={{
          borderTop: "1px solid var(--border-subtle)",
          padding: "16px 0",
          color: "var(--text-muted)",
          fontSize: "12px",
          textAlign: "center",
        }}
      >
        <div className="app-container">
          Quran Reels Automation Operations Dashboard • V1 (Europe/Berlin)
        </div>
      </footer>
    </div>
  );
}
