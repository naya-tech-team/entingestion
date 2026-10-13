// Pure helpers for the Operations pages: ages, status order, selection caps, request building
// and action summaries. No React, no fetch: unit-tested in ops.test.ts.

import type { ActionResponse, OpsQueue, OpsQueueDepth, SearchRequest } from "./types";

export const OVERVIEW_REFRESH_MS = 15_000;
/** After a confirmed re-run, a finished page keeps polling this long to pick up the new run. */
export const RERUN_WATCH_MS = 120_000;

/** The BFF's caps (it answers 422 beyond them). */
export const MAX_RERUN_IDS = 100;
export const MAX_REDRIVE = 200;
export const DEFAULT_REDRIVE = 50;
export const MIN_K = 1;
export const MAX_K = 50;
export const DEFAULT_K = 10;
export const MAX_QUERY_CHARS = 2000;
export const SNIPPET_CHARS = 400;

export const DEFAULT_FAILED_STATUS = "FAILED_PERMANENT";

/** Pipeline order for the overview; statuses not listed sort after these, by name. */
export const OPS_STATUS_ORDER = [
  "CLAIMED",
  "FETCHING",
  "EXTRACTING",
  "NORMALIZING",
  "CHUNKING",
  "EMBEDDING",
  "VALIDATING",
  "STAGED",
  "INDEXING",
  "FAILED_RETRYABLE",
  "FAILED_PERMANENT",
] as const;

export function orderStatuses(statuses: Iterable<string>): string[] {
  const rank = (s: string) => {
    const i = (OPS_STATUS_ORDER as readonly string[]).indexOf(s);
    return i === -1 ? OPS_STATUS_ORDER.length : i;
  };
  return [...new Set(statuses)].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

/** The status picker's options: the overview's statuses, always with the failed ones and the current one. */
export function statusOptions(overviewStatuses: Iterable<string>, current: string): string[] {
  return orderStatuses([...overviewStatuses, "FAILED_RETRYABLE", DEFAULT_FAILED_STATUS, ...(current ? [current] : [])]);
}

export const QUEUES: readonly { key: string; label: string; dlq: boolean }[] = [
  { key: "ingest", label: "Ingest queue", dlq: false },
  { key: "ingest_dlq", label: "Ingest DLQ", dlq: true },
  { key: "backfill", label: "Backfill queue", dlq: false },
  { key: "backfill_dlq", label: "Backfill DLQ", dlq: true },
];

export function queueDepth(queue: OpsQueue | null | undefined): OpsQueueDepth | null {
  if (!queue || "error" in queue) return null;
  return typeof queue.visible === "number" ? queue : null;
}

/**
 * How long ago `iso` was, compactly: "just now", "42 s ago", "5 min ago", "3 h 12 min ago",
 * "4 d 2 h ago". A time in the future (clock skew) reads "just now"; a missing or bad one "-".
 */
export function relativeAge(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "-";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "-";
  const seconds = Math.floor((now - then) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min ago` : `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} d ${hours % 24} h ago` : `${days} d ago`;
}

// --- selection ---

export interface SelectionCheck {
  canRun: boolean;
  message: string;
}

/** Re-run selected: at least one and at most MAX_RERUN_IDS articles. */
export function checkSelection(count: number, cap: number = MAX_RERUN_IDS): SelectionCheck {
  if (count <= 0) return { canRun: false, message: `Select up to ${cap} articles` };
  if (count > cap) return { canRun: false, message: `${count} selected: at most ${cap} per re-run` };
  return { canRun: true, message: `${count} selected (at most ${cap})` };
}

export function toggleOne(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** "Select all" over the rows on the page: selects them all, or clears them when all are selected. */
export function toggleAll(selected: ReadonlySet<string>, pageIds: readonly string[]): Set<string> {
  const next = new Set(selected);
  if (allSelected(selected, pageIds)) for (const id of pageIds) next.delete(id);
  else for (const id of pageIds) next.add(id);
  return next;
}

export function allSelected(selected: ReadonlySet<string>, pageIds: readonly string[]): boolean {
  return pageIds.length > 0 && pageIds.every((id) => selected.has(id));
}

export function someSelected(selected: ReadonlySet<string>, pageIds: readonly string[]): boolean {
  return pageIds.some((id) => selected.has(id)) && !allSelected(selected, pageIds);
}

// --- form parsing ---

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

/** An integer in [min, max]; blank gives the default. */
export function parseBoundedInt(text: string, min: number, max: number, fallback: number, name: string): Parsed<number> {
  const t = text.trim();
  if (t === "") return { ok: true, value: fallback };
  const n = Number(t);
  if (!Number.isInteger(n) || n < min || n > max) return { ok: false, error: `${name} must be a whole number from ${min} to ${max}` };
  return { ok: true, value: n };
}

export function buildSearchRequest(tenantId: string, queryText: string, kText: string): Parsed<SearchRequest> {
  const tenant = tenantId.trim();
  if (!tenant) return { ok: false, error: "Choose a tenant" };
  const q = queryText.trim();
  if (!q) return { ok: false, error: "Type a query" };
  if (q.length > MAX_QUERY_CHARS) return { ok: false, error: `The query is longer than ${MAX_QUERY_CHARS} characters` };
  const k = parseBoundedInt(kText, MIN_K, MAX_K, DEFAULT_K, "k");
  if (!k.ok) return k;
  return { ok: true, value: { tenant_id: tenant, query: q, k: k.value } };
}

export interface RedriveRequest {
  error_code?: string;
  max: number;
}

export function buildRedriveRequest(errorCode: string, maxText: string): Parsed<RedriveRequest> {
  const max = parseBoundedInt(maxText, 1, MAX_REDRIVE, DEFAULT_REDRIVE, "Max");
  if (!max.ok) return max;
  const code = errorCode.trim();
  return { ok: true, value: code ? { error_code: code, max: max.value } : { max: max.value } };
}

/** Distinct tenant ids of the caller's sources, sorted. */
export function distinctTenants(items: readonly { tenant_id?: string | null }[]): string[] {
  return [...new Set(items.map((i) => i.tenant_id ?? "").filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

export function truncate(text: string | null | undefined, max: number = SNIPPET_CHARS): string {
  if (!text) return "";
  const t = text.replace(/\s+/g, " ").trim();
  return t.length <= max ? t : `${t.slice(0, max - 1).trimEnd()}…`;
}

export function formatScore(score: number | null | undefined): string {
  return typeof score === "number" && Number.isFinite(score) ? score.toFixed(4) : "-";
}

// --- action previews and results ---

export interface ActionSummary {
  dryRun: boolean;
  count: number;
  queued: number | null;
  articleIds: string[];
  notFound: string[];
  errorCode: string | null;
  dlq: { messages: number; moved: boolean } | null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** A defensive reading of an ingest/redrive response (dry run or real). */
export function summarizeAction(resp: ActionResponse | null | undefined): ActionSummary {
  const r = (resp ?? {}) as Partial<ActionResponse>;
  const messages = Array.isArray(r.messages) ? r.messages : [];
  const articleIds = messages
    .map((m) => (m && typeof m === "object" ? m.article_id : undefined))
    .filter((id): id is string => typeof id === "string");
  const dlq =
    r.dlq && typeof r.dlq === "object" && typeof r.dlq.messages === "number"
      ? { messages: r.dlq.messages, moved: Boolean(r.dlq.moved) }
      : null;
  return {
    dryRun: r.dry_run !== false,
    count: typeof r.count === "number" ? r.count : articleIds.length,
    queued: typeof r.queued === "number" ? r.queued : null,
    articleIds,
    notFound: strings(r.not_found),
    errorCode: typeof r.error_code === "string" ? r.error_code : null,
    dlq,
  };
}

/** Confirm is offered only when the preview would do something. */
export function previewHasWork(resp: ActionResponse | null | undefined): boolean {
  const s = summarizeAction(resp);
  return s.count > 0 || (s.dlq !== null && s.dlq.messages > 0);
}

export function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
