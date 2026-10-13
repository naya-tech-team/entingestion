// The request logic behind the data hooks, without React: one request slot per hook (single
// flight or replace), how a result settles into state, when to poll and how pages append.
// Unit-tested in request.test.ts (node environment).

import { ApiError } from "../api";

/** A request's result: ok with a value, or failed with an error. */
export type Outcome<T> = { ok: true; value: T } | { ok: false; error: unknown };

/**
 * The request a hook has in flight, if any.
 * - `begin()` is single flight: it returns null while a request runs (the caller skips).
 * - `replace()` aborts the running request and starts another.
 * - `finish(ctrl)` is true when `ctrl` is still the current, unaborted request (its result
 *   applies), and frees the slot; a superseded or aborted request gets false.
 */
export class RequestSlot {
  private current: AbortController | null = null;

  get busy(): boolean {
    return this.current !== null;
  }

  begin(): AbortController | null {
    if (this.current) return null;
    this.current = new AbortController();
    return this.current;
  }

  replace(): AbortController {
    this.current?.abort();
    this.current = new AbortController();
    return this.current;
  }

  finish(ctrl: AbortController): boolean {
    if (ctrl !== this.current) return false;
    this.current = null;
    return !ctrl.signal.aborted;
  }

  abort(): void {
    this.current?.abort();
    this.current = null;
  }
}

/**
 * Runs `fn` in `slot`. Resolves to its outcome, or null when it was skipped (single flight and
 * busy) or aborted or superseded on the way (its result must not apply). `onStart` runs only
 * when the request actually starts.
 */
export async function runInSlot<T>(
  slot: RequestSlot,
  fn: (signal: AbortSignal) => Promise<T>,
  options: { replace?: boolean; onStart?: () => void } = {},
): Promise<Outcome<T> | null> {
  const ctrl = options.replace ? slot.replace() : slot.begin();
  if (!ctrl) return null;
  options.onStart?.();
  let outcome: Outcome<T>;
  try {
    outcome = { ok: true, value: await fn(ctrl.signal) };
  } catch (error) {
    outcome = { ok: false, error };
  }
  return slot.finish(ctrl) ? outcome : null;
}

export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

export interface LoadState<T> {
  data: T | null;
  error: unknown;
  /** The last load answered 404 and the caller tolerates it (`notFoundOk`). */
  notFound: boolean;
}

/**
 * The state after a load: a success replaces the data and clears the error. A tolerated 404
 * clears both and sets `notFound`. Any other error keeps the last data (a refresh failing does
 * not blank the page).
 */
export function settle<T>(prev: LoadState<T>, outcome: Outcome<T>, notFoundOk: boolean): LoadState<T> {
  if (outcome.ok) return { data: outcome.value, error: null, notFound: false };
  if (notFoundOk && isNotFound(outcome.error)) return { data: null, error: null, notFound: true };
  return { ...prev, error: outcome.error };
}

/**
 * The polling interval, or null for none: no `refreshMs`, the session expired (polling would
 * only repeat the 401), or the data says it is final (`stopWhen`). Without data yet, polling
 * continues.
 */
export function pollDelay<T>(
  refreshMs: number | null | undefined,
  authLost: boolean,
  data: T | null,
  stopWhen?: (data: T) => boolean,
): number | null {
  if (refreshMs === null || refreshMs === undefined || authLost) return null;
  if (data !== null && stopWhen?.(data)) return null;
  return refreshMs;
}

/** One page of a token-paged list, as the BFF returns it. */
export interface Page<T> {
  items: T[];
  next_token: string | null;
}

/** The first page (no token) replaces the list; a later page appends to it. */
export function appendPage<T>(current: readonly T[], items: readonly T[], token: string | null): T[] {
  return token ? [...current, ...items] : [...items];
}
