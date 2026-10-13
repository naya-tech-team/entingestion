import { useCallback, useEffect, useRef, useState } from "react";
import { api, AuthError } from "../api";
import { type LoadState, pollDelay, RequestSlot, runInSlot, settle } from "./request";
import { useInterval } from "./useInterval";

export interface LoaderOptions<T> {
  /** Reload every `refreshMs` (skipped while the tab is hidden). Unset: load once. */
  refreshMs?: number | null;
  /** Stop polling while this is true of the current data (re-read on every render). */
  stopWhen?: (data: T) => boolean;
  /** A 404 is a state (`notFound`, data null), not an error. */
  notFoundOk?: boolean;
}

export interface ApiState<T> {
  data: T | null;
  error: unknown;
  notFound: boolean;
  loading: boolean;
  updatedAt: Date | null;
  /** The session expired (401): polling stops until the page is reloaded. */
  authLost: boolean;
  /** Auto-refresh is running. */
  polling: boolean;
  reload: () => void;
}

/** Loads the value; gets the abort signal and the current data (to keep parts that failed). */
export type Fetcher<T> = (signal: AbortSignal, previous: T | null) => Promise<T>;

interface State<T> extends LoadState<T> {
  loading: boolean;
  updatedAt: Date | null;
}

const EMPTY = { data: null, error: null, notFound: false, loading: false } as const;

/**
 * Loads `fetcher` on mount and whenever `key` changes (the previous request is aborted and the
 * data cleared), then polls per `options`. One request at a time: a reload while one is in
 * flight is skipped. A null key loads nothing.
 */
export function useLoader<T>(key: string | null, fetcher: Fetcher<T>, options: LoaderOptions<T> = {}): ApiState<T> {
  const [state, setState] = useState<State<T>>({ ...EMPTY, updatedAt: null });
  const [slot] = useState(() => new RequestSlot());
  const fetcherRef = useRef(fetcher);
  const notFoundOk = useRef(Boolean(options.notFoundOk));
  const dataRef = useRef<T | null>(null);
  useEffect(() => {
    fetcherRef.current = fetcher;
    notFoundOk.current = Boolean(options.notFoundOk);
  });
  useEffect(() => {
    dataRef.current = state.data;
  }, [state.data]);

  const load = useCallback(async () => {
    if (key === null) return;
    const outcome = await runInSlot(slot, (signal) => fetcherRef.current(signal, dataRef.current), {
      onStart: () => setState((s) => (s.loading ? s : { ...s, loading: true })),
    });
    if (!outcome) return; // skipped, aborted or superseded
    setState((s) => ({ ...settle(s, outcome, notFoundOk.current), loading: false, updatedAt: new Date() }));
  }, [key, slot]);

  useEffect(() => {
    dataRef.current = null;
    setState((s) =>
      s.data === null && s.error === null && !s.notFound && !s.loading ? s : { ...s, ...EMPTY },
    );
    void load();
    return () => slot.abort();
  }, [load, slot]);

  const authLost = state.error instanceof AuthError;
  const delay = key === null ? null : pollDelay(options.refreshMs, authLost, state.data, options.stopWhen);
  useInterval(() => void load(), delay);
  const reload = useCallback(() => void load(), [load]);

  return { ...state, authLost, polling: delay !== null, reload };
}

/** GET `path` (same-origin /api/*) with useLoader's loading, abort and polling. */
export function useApi<T>(path: string | null, options?: LoaderOptions<T>): ApiState<T> {
  return useLoader<T>(path, (signal) => api.get<T>(path ?? "", signal), options);
}
