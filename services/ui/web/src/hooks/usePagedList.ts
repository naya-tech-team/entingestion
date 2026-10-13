import { type DependencyList, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { appendPage, type Page, RequestSlot, runInSlot } from "./request";

export interface PagedList<T> {
  items: T[];
  nextToken: string | null;
  /** Fetches the next page and appends it (no-op without a next token). */
  loadMore: () => void;
  /** Fetches the first page again; the current rows stay until it arrives. */
  reload: () => void;
  /** Clears the list and fetches the first page (what a change of `deps` does). */
  reset: () => void;
  loading: boolean;
  /** At least one page arrived since the last reset. */
  loaded: boolean;
  error: unknown;
}

/**
 * A token-paged list (`{items, next_token}`), fetched from `buildPath(token)` (null: nothing to
 * fetch yet, e.g. no source chosen). A change of `deps` clears the list and fetches the first
 * page. A new fetch aborts the one in flight, so a late page never lands on a newer list.
 */
export function usePagedList<T>(buildPath: (token: string | null) => string | null, deps: DependencyList): PagedList<T> {
  const [items, setItems] = useState<T[]>([]);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [slot] = useState(() => new RequestSlot());
  const buildRef = useRef(buildPath);
  useEffect(() => {
    buildRef.current = buildPath;
  });

  const load = useCallback(
    async (token: string | null) => {
      const path = buildRef.current(token);
      if (path === null) return;
      const outcome = await runInSlot(slot, (signal) => api.get<Page<T>>(path, signal), {
        replace: true,
        onStart: () => {
          setLoading(true);
          setError(null);
        },
      });
      if (!outcome) return; // aborted or superseded
      if (outcome.ok) {
        setItems((cur) => appendPage(cur, outcome.value.items, token));
        setNextToken(outcome.value.next_token);
        setLoaded(true);
      } else {
        setError(outcome.error);
      }
      setLoading(false);
    },
    [slot],
  );

  const reset = useCallback(() => {
    slot.abort();
    setItems([]);
    setNextToken(null);
    setLoaded(false);
    setLoading(false);
    setError(null);
    void load(null);
  }, [load, slot]);

  useEffect(() => {
    reset();
    return () => slot.abort();
    // The caller's deps decide when the list starts over; buildPath is read through a ref.
  }, deps);

  return {
    items,
    nextToken,
    loadMore: () => {
      if (nextToken) void load(nextToken);
    },
    reload: () => void load(null),
    reset,
    loading,
    loaded,
    error,
  };
}
