"use client";

import React, { useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  LayoutDashboard,
  History,
  Settings,
  LogOut,
  Sparkles,
} from "lucide-react";

export function Navbar({ automationEnabled }: { automationEnabled: boolean }) {
  const pathname = usePathname();
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
      router.push("/login");
      router.refresh();
    } catch {
      router.push("/login");
    }
  };

  return (
    <header className="navbar">
      <div className="app-container">
        <div className="navbar-inner">
          <div style={{ display: "flex", alignItems: "center", gap: "20px" }}>
            <Link href="/dashboard" className="nav-brand">
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: "28px",
                  height: "28px",
                  borderRadius: "6px",
                  background: "var(--primary-subtle)",
                  color: "var(--primary)",
                }}
              >
                <Sparkles size={16} />
              </div>
              <span>Quran Reels Operations</span>
            </Link>

            <span
              className={`badge ${
                automationEnabled ? "badge-success" : "badge-neutral"
              }`}
              style={{ fontSize: "11px" }}
            >
              {automationEnabled ? "● Automation Enabled" : "○ Automation Disabled"}
            </span>
          </div>

          <nav className="nav-links">
            <Link
              href="/dashboard"
              className={`nav-link ${pathname === "/dashboard" ? "active" : ""}`}
            >
              <LayoutDashboard size={15} />
              <span>Overview</span>
            </Link>
            <Link
              href="/runs"
              className={`nav-link ${pathname.startsWith("/runs") ? "active" : ""}`}
            >
              <History size={15} />
              <span>Runs</span>
            </Link>
            <Link
              href="/settings"
              className={`nav-link ${pathname === "/settings" ? "active" : ""}`}
            >
              <Settings size={15} />
              <span>Settings</span>
            </Link>

            <button
              onClick={handleLogout}
              disabled={loggingOut}
              className="btn btn-secondary nav-logout"
              style={{
                padding: "4px 10px",
                fontSize: "12px",
                marginLeft: "8px",
                height: "30px",
              }}
              title="Sign Out"
            >
              <LogOut size={13} />
              <span>{loggingOut ? "Exiting..." : "Logout"}</span>
            </button>
          </nav>
        </div>
      </div>
    </header>
  );
}
