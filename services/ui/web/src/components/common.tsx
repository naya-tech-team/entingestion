import type { ReactNode } from "react";
import { AuthError, errorMessage } from "../api";
import { statusTone } from "../status";

export function StatusBadge({ status }: { status: string | null | undefined }) {
  return <span className={`badge badge-${statusTone(status)}`}>{status ?? "-"}</span>;
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const auth = error instanceof AuthError;
  return (
    <div className={`alert ${auth ? "alert-warn" : "alert-error"}`} role="alert">
      {errorMessage(error)}
    </div>
  );
}

export function Loading({ label = "Loading..." }: { label?: string }) {
  return (
    <p className="muted" role="status">
      {label}
    </p>
  );
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
