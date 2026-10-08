export const palette = {
  blue: "#0A3161", // American Blue — documented / primary
  white: "#E3E2DE", // Chinese White — canvas
  fog: "#CAC8C5", // Foggy — grid, dividers, muted
  red: "#A9143C", // Red Kiss — needs attention
  gold: "#D7A942", // San Antonio — partially documented
  brown: "#A56246", // Burnt Brown — open change requests
};

export type Health = "good" | "partial" | "poor";

export function health(coverage: number, target: number): Health {
  if (coverage >= target) return "good";
  if (coverage >= target / 2) return "partial";
  return "poor";
}

export const healthColor: Record<Health, string> = {
  good: palette.blue,
  partial: palette.gold,
  poor: palette.red,
};

export const healthLabel: Record<Health, string> = {
  good: "Documented",
  partial: "Partially documented",
  poor: "Needs attention",
};

export const pct = (x: number) => `${Math.round(x * 100)}%`;

export function initials(email: string | null | undefined): string {
  if (!email) return "?";
  const local = email.split("@")[0];
  const parts = local.split(/[._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || local[0].toUpperCase();
}

export function when(ts: string | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z");
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return d.toLocaleDateString();
}

export const statusLabel: Record<string, string> = {
  PENDING: "Pending review",
  APPROVED: "Approved · queued",
  APPLIED: "Applied",
  REJECTED: "Rejected",
  FAILED: "Failed",
  WITHDRAWN: "Withdrawn",
  SUPERSEDED: "Superseded",
};
