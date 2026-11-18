"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Facebook, Instagram, RefreshCw, ShieldCheck, Unlink } from "lucide-react";

type MetaStatus = {
  connected: boolean;
  connectedAt?: string;
  graphApiVersion?: string;
  page?: { id: string; name: string };
  instagram?: { id: string; username: string | null; name: string | null } | null;
  tokenExpiresAt?: string | null;
  error?: string;
};

export function MetaAccounts() {
  const [status, setStatus] = useState<MetaStatus | null>(null);
  const [busy, setBusy] = useState<"load" | "test" | "disconnect" | null>("load");
  const [message, setMessage] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setBusy("load");
    try {
      const response = await fetch("/api/meta/status", { cache: "no-store" });
      const data = await response.json() as MetaStatus;
      setStatus(response.ok ? data : { connected: false, error: data.error || "Status unavailable." });
    } catch {
      setStatus({ connected: false, error: "Meta status could not be loaded." });
    } finally {
      setBusy(null);
    }
  }, []);

  useEffect(() => { void loadStatus(); }, [loadStatus]);

  useEffect(() => {
    const parameters = new URLSearchParams(window.location.search);
    const result = parameters.get("meta");
    if (result === "connected") {
      setMessage("Meta account connection saved securely.");
    } else if (result === "error") {
      const reason = parameters.get("reason") || "connection_failed";
      const explanations: Record<string, string> = {
        unauthorized: "The administrator session expired. Sign in and reconnect.",
        invalid_state: "Meta connection was rejected because the security state did not match. Please reconnect.",
        access_denied: "Meta access was not granted.",
        missing_code: "Meta did not return an authorization code.",
        connection_failed: "Meta connection could not be completed. Check the app, permissions, Page selection, and callback URL.",
      };
      setMessage(explanations[reason] || explanations.connection_failed);
    }
    if (result) {
      parameters.delete("meta");
      parameters.delete("reason");
      const query = parameters.toString();
      window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
    }
  }, []);

  const testConnection = async () => {
    setBusy("test");
    setMessage(null);
    try {
      const response = await fetch("/api/meta/test", { method: "POST" });
      const result = await response.json() as { facebook?: boolean; instagram?: boolean; error?: string };
      if (!response.ok) throw new Error(result.error || "Connection test failed.");
      setMessage(`Facebook verified${status?.instagram ? (result.instagram ? "; Instagram verified." : "; Instagram needs reconnection.") : "."}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Connection test failed.");
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async () => {
    if (!window.confirm("Remove the saved Meta credentials from this Dashboard? This does not revoke the Meta app grant in Meta Business settings.")) return;
    setBusy("disconnect");
    setMessage(null);
    try {
      const response = await fetch("/api/meta/disconnect", { method: "POST" });
      if (!response.ok) throw new Error("Disconnect failed.");
      setStatus({ connected: false });
      setMessage("Saved Meta credentials removed locally. The Meta app grant was not revoked; manage that separately in Meta Business settings if required.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Disconnect failed.");
    } finally {
      setBusy(null);
    }
  };

  const connected = status?.connected === true;
  return (
    <div className="card">
      <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>Meta Accounts</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
        Connect a Facebook Page and its linked Instagram Professional account through secure server-side OAuth. Tokens are never shown in the browser.
      </p>

      <div className="meta-account-grid">
        <div className="meta-account-card">
          <div className="meta-account-heading"><Facebook size={17} /><strong>Facebook Page</strong></div>
          {connected && status.page ? (
            <>
              <span className="badge badge-success">Connected</span>
              <div className="meta-account-name">{status.page.name}</div>
              <code className="meta-account-id">Page ID: {status.page.id}</code>
            </>
          ) : (
            <><span className="badge badge-neutral">Not connected</span><p className="form-hint">A managed Facebook Page is required for Reel publishing.</p></>
          )}
        </div>

        <div className="meta-account-card">
          <div className="meta-account-heading"><Instagram size={17} /><strong>Instagram</strong><span className="badge badge-neutral">OPTIONAL</span></div>
          {connected && status.instagram ? (
            <>
              <span className="badge badge-success">Connected</span>
              <div className="meta-account-name">{status.instagram.username ? `@${status.instagram.username}` : status.instagram.name || "Professional account"}</div>
              <code className="meta-account-id">Account ID: {status.instagram.id}</code>
            </>
          ) : (
            <><span className="badge badge-neutral">Not connected</span><p className="form-hint">Link a Business or Creator account to the selected Facebook Page.</p></>
          )}
        </div>
      </div>

      {status?.error && <p className="meta-account-message meta-account-error">{status.error}</p>}
      {message && <p className="meta-account-message">{message}</p>}
      <div className="meta-account-actions">
        {!connected ? (
          <>
            <a className="btn btn-primary" href="/api/meta/connect"><Facebook size={14} /> Connect Facebook</a>
            <a className="btn btn-secondary" href="/api/meta/connect"><Instagram size={14} /> Connect Instagram</a>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-primary" onClick={testConnection} disabled={busy !== null}>
              {busy === "test" ? <RefreshCw size={14} className="spin" /> : <ShieldCheck size={14} />} Test Connection
            </button>
            <a className="btn btn-secondary" href="/api/meta/connect"><RefreshCw size={14} /> Reconnect</a>
            <button type="button" className="btn btn-danger" onClick={disconnect} disabled={busy !== null}><Unlink size={14} /> Disconnect</button>
          </>
        )}
        {busy === "load" && <span className="form-hint">Checking encrypted connection…</span>}
      </div>
      {connected && <p className="form-hint" style={{ marginTop: "10px" }}>Graph API {status.graphApiVersion}. Facebook remains primary; Instagram is optional.</p>}
      {connected && <p className="form-hint">Disconnect removes this Dashboard&apos;s encrypted credential object; it does not revoke the authorization grant in Meta Business settings.</p>}
    </div>
  );
}
