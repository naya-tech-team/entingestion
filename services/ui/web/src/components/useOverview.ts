import { useCallback, useEffect, useRef, useState } from "react";
import { api, AuthError } from "../api";
import type { OpsOverview } from "../types";
import { useInterval } from "./common";

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
  const [overview, setOverview] = useState<OpsOverview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    const ctrl = new AbortController();
    inFlight.current = ctrl;
    setLoading(true);
    try {
      setOverview(await api.get<OpsOverview>("/api/ops/overview", ctrl.signal));
      setError(null);
    } catch (err) {
      if (ctrl.signal.aborted) return;
      setError(err);
    }
    setUpdatedAt(new Date());
    setLoading(false);
    inFlight.current = null;
  }, []);

  useEffect(() => {
    void load();
    return () => {
      inFlight.current?.abort();
      inFlight.current = null;
    };
  }, [load]);

  const authLost = error instanceof AuthError;
  useInterval(() => void load(), authLost ? null : refreshMs);

  return { overview, error, loading, updatedAt, authLost, reload: () => void load() };
}
