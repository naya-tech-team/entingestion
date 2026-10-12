// Display formatting: sizes, durations, local times, short principals.

const UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "-";
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit++;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(value < 10 ? 1 : 0)} ${UNITS[unit]}`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "-";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes} min ${rest} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : dateTime.format(d);
}

/** An IAM principal shortened for a table: the role of an assumed-role ARN, else the last part. */
export function shortPrincipal(principal: string | null | undefined): string {
  if (!principal) return "-";
  const role = /assumed-role\/([^/]+)/.exec(principal);
  if (role?.[1]) return role[1];
  const parts = principal.split(/[/:]/).filter(Boolean);
  return parts[parts.length - 1] ?? principal;
}

export function percent(fraction: number): string {
  return `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`;
}

export function defaultBatchName(now: Date = new Date()): string {
  return `UI upload ${now.toLocaleString()}`;
}

/** A compact one-line summary of a step's metrics object. */
export function metricsSummary(metrics: Record<string, unknown> | null | undefined): string {
  if (!metrics) return "";
  return Object.entries(metrics)
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .slice(0, 6)
    .map(([k, v]) => `${k}=${typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : String(v)}`)
    .join(", ");
}
