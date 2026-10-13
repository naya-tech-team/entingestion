import type { SourceAccess, SourcesResponse } from "../types";
import { useApi } from "./useApi";

export interface SourcesState {
  /** The caller's sources; null until loaded. */
  sources: SourceAccess[] | null;
  error: unknown;
}

/** GET /api/sources once: the sources the caller may read or write. */
export function useSources(): SourcesState {
  const { data, error } = useApi<SourcesResponse>("/api/sources");
  return { sources: data?.items ?? null, error };
}
