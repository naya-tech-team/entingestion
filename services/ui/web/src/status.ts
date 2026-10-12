// Pure helpers for the tracking pages: when to stop polling, step order, badge tones.

import type { ArticleStep, Process, ProcessSummary } from "./types";

export const POLL_MS = 5000;

/** Pipeline order. Steps not listed here sort after these, by sort key. */
export const STEP_ORDER = [
  "CLAIM",
  "FETCH",
  "EXTRACT",
  "OCR",
  "NORMALIZE",
  "CLASSIFY",
  "CHUNK",
  "EMBED",
  "VALIDATE",
  "INDEX",
  "FINALIZE",
] as const;

export function orderSteps<T extends Pick<ArticleStep, "step" | "sk">>(steps: readonly T[]): T[] {
  const rank = (s: T) => {
    const i = (STEP_ORDER as readonly string[]).indexOf(s.step);
    return i === -1 ? STEP_ORDER.length : i;
  };
  return [...steps].sort((a, b) => rank(a) - rank(b) || (a.sk < b.sk ? -1 : a.sk > b.sk ? 1 : 0));
}

export const SUMMARY_DONE = new Set(["COMPLETE", "COMPLETE_WITH_ERRORS"]);

/** The process page stops refreshing once ingestion is complete and the producer has settled. */
export function processFinished(summary: ProcessSummary | null, process: Process | null): boolean {
  if (!summary || !SUMMARY_DONE.has(summary.status)) return false;
  return process === null ? true : process.settled;
}

// Ingestion statuses that won't change without a new upload or a rerun.
const ARTICLE_FINAL = new Set(["INDEXED", "FAILED_PERMANENT", "DELETED"]);

/** True while an article may still change (running, staged, retrying...). */
export function articleActive(status: string | null | undefined): boolean {
  if (!status) return true;
  return !(ARTICLE_FINAL.has(status) || status.startsWith("SKIPPED"));
}

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export function statusTone(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  if (["INDEXED", "REGISTERED", "COMPLETE", "SUCCEEDED", "METADATA_UPDATED", "CLOSED"].includes(status)) return "ok";
  if (status.startsWith("SKIPPED") || ["SUPERSEDED", "EXPIRED", "COMPLETE_WITH_ERRORS", "FAILED_RETRYABLE", "RETRY"].includes(status))
    return "warn";
  if (status.startsWith("FAILED") || ["REJECTED", "ERROR", "TIMED_OUT", "ABORTED"].includes(status)) return "bad";
  if (["DELETED", "STALE", "DELETE_DEFERRED"].includes(status)) return "neutral";
  return "info";
}

/** Articles that reached an outcome (indexed, skipped or failed) out of those registered. */
export function processProgress(
  summary: ProcessSummary | null,
  process: Process | null,
): { done: number; total: number } {
  const counts = summary?.counts ?? { indexed: 0, skipped: 0, failed: 0 };
  const done = counts.indexed + counts.skipped + counts.failed;
  const total = process?.registered_count ?? summary?.producer.registered_count ?? 0;
  return { done, total: Math.max(total, done) };
}
