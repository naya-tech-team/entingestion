import type { OpsOverview } from "../types";
import { useApi } from "./useApi";

export interface OverviewState {
  overview: OpsOverview | null;
  error: unknown;
  loading: boolean;
  updatedAt: Date | null;
  authLost: boolean;
  reload: () => void;
}

/** GET /api/ops/overview on mount, then every `refreshMs` (null: no auto-refresh). */
export function useOverview(refreshMs: number | null): OverviewState {
  const { data, error, loading, updatedAt, authLost, reload } = useApi<OpsOverview>("/api/ops/overview", { refreshMs });
  return { overview: data, error, loading, updatedAt, authLost, reload };
}
