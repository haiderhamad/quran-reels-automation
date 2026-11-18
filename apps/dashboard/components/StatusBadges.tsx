import React from "react";
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Clock,
  MinusCircle,
  HelpCircle,
} from "lucide-react";

export function GenerationStatusBadge({ status }: { status: string }) {
  switch (status) {
    case "SUCCESS":
      return (
        <span className="badge badge-success">
          <CheckCircle2 size={13} />
          <span>Success</span>
        </span>
      );
    case "FAILED":
      return (
        <span className="badge badge-failed">
          <XCircle size={13} />
          <span>Failed</span>
        </span>
      );
    case "STARTED":
      return (
        <span className="badge badge-blocked">
          <Clock size={13} />
          <span>Started</span>
        </span>
      );
    default:
      return (
        <span className="badge badge-neutral">
          <MinusCircle size={13} />
          <span>{status || "Unknown"}</span>
        </span>
      );
  }
}

export function PlatformStatusBadge({
  platform,
  status,
  isPrimary = false,
}: {
  platform: "facebook" | "instagram";
  status: string;
  isPrimary?: boolean;
}) {
  const platformName = platform === "facebook" ? "Facebook" : "Instagram";
  const primaryTag = isPrimary ? " (Primary)" : " (Optional)";

  switch (status) {
    case "ACCEPTED":
      return (
        <span className="badge badge-unknown" title={`${platformName}${primaryTag}: Accepted by Buffer; platform delivery has not been confirmed`}>
          <Clock size={13} />
          <span>{platformName}: Accepted by Buffer</span>
        </span>
      );
    case "SUCCESS":
      return (
        <span className="badge badge-success" title={`${platformName}${primaryTag}: Published`}>
          <CheckCircle2 size={13} />
          <span>{platformName}: Published</span>
        </span>
      );
    case "FAILED":
      return (
        <span
          className={`badge ${isPrimary ? "badge-failed" : "badge-blocked"}`}
          title={`${platformName}${primaryTag}: Publishing Failed`}
        >
          <XCircle size={13} />
          <span>{platformName}: Failed</span>
        </span>
      );
    case "BLOCKED":
      return (
        <span
          className="badge badge-blocked"
          title={`${platformName}${primaryTag}: Rights Gated`}
        >
          <AlertTriangle size={13} />
          <span>{platformName}: Blocked (Rights)</span>
        </span>
      );
    case "UNKNOWN":
      return (
        <span
          className="badge badge-unknown"
          title={`${platformName}${primaryTag}: Ambiguous outcome — manual check required`}
        >
          <HelpCircle size={13} />
          <span>{platformName}: Manual Check Needed</span>
        </span>
      );
    case "NOT_CONFIGURED":
      return (
        <span
          className="badge badge-neutral"
          title={`${platformName}${primaryTag}: Credentials not configured`}
        >
          <MinusCircle size={13} />
          <span>{platformName}: Not Configured</span>
        </span>
      );
    case "NOT_ATTEMPTED":
    default:
      return (
        <span
          className="badge badge-neutral"
          title={`${platformName}${primaryTag}: Not Attempted`}
        >
          <MinusCircle size={13} />
          <span>{platformName}: Not Attempted</span>
        </span>
      );
  }
}
