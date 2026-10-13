import { describe, expect, it } from "vitest";
import type { ActionResponse } from "./types";
import {
  allSelected,
  buildRedriveRequest,
  buildSearchRequest,
  checkSelection,
  distinctTenants,
  formatScore,
  MAX_RERUN_IDS,
  orderStatuses,
  previewHasWork,
  queueDepth,
  relativeAge,
  someSelected,
  statusOptions,
  summarizeAction,
  toggleAll,
  toggleOne,
  truncate,
} from "./ops";

const NOW = Date.parse("2026-10-12T12:00:00Z");
const ago = (ms: number) => new Date(NOW - ms).toISOString();

describe("relativeAge", () => {
  it("formats compact ages", () => {
    expect(relativeAge(ago(2_000), NOW)).toBe("just now");
    expect(relativeAge(ago(42_000), NOW)).toBe("42 s ago");
    expect(relativeAge(ago(5 * 60_000 + 10_000), NOW)).toBe("5 min ago");
    expect(relativeAge(ago(3 * 3_600_000), NOW)).toBe("3 h ago");
    expect(relativeAge(ago(3 * 3_600_000 + 12 * 60_000), NOW)).toBe("3 h 12 min ago");
    expect(relativeAge(ago(4 * 86_400_000), NOW)).toBe("4 d ago");
    expect(relativeAge(ago(4 * 86_400_000 + 2 * 3_600_000), NOW)).toBe("4 d 2 h ago");
  });

  it("handles missing, bad and future times", () => {
    expect(relativeAge(null, NOW)).toBe("-");
    expect(relativeAge("not a date", NOW)).toBe("-");
    expect(relativeAge(new Date(NOW + 60_000).toISOString(), NOW)).toBe("just now");
  });
});

describe("selection", () => {
  it("needs 1 to 100 articles", () => {
    expect(checkSelection(0).canRun).toBe(false);
    expect(checkSelection(1).canRun).toBe(true);
    expect(checkSelection(MAX_RERUN_IDS).canRun).toBe(true);
    const over = checkSelection(MAX_RERUN_IDS + 1);
    expect(over.canRun).toBe(false);
    expect(over.message).toContain("at most 100");
  });

  it("toggles rows and select-all on the page", () => {
    let sel: Set<string> = new Set();
    sel = toggleOne(sel, "a");
    expect([...sel]).toEqual(["a"]);
    expect(someSelected(sel, ["a", "b"])).toBe(true);
    sel = toggleAll(sel, ["a", "b"]);
    expect(allSelected(sel, ["a", "b"])).toBe(true);
    expect(someSelected(sel, ["a", "b"])).toBe(false);
    sel = toggleAll(sel, ["a", "b"]);
    expect(sel.size).toBe(0);
    expect(allSelected(sel, [])).toBe(false);
  });

  it("keeps rows of other pages when toggling all", () => {
    const sel = toggleAll(new Set(["x"]), ["a", "b"]);
    expect([...sel].sort()).toEqual(["a", "b", "x"]);
    expect([...toggleAll(sel, ["a", "b"])]).toEqual(["x"]);
  });

  it("caps a large select-all", () => {
    const ids = Array.from({ length: 150 }, (_, i) => `a${i}`);
    expect(checkSelection(toggleAll(new Set(), ids).size).canRun).toBe(false);
  });
});

describe("buildSearchRequest", () => {
  it("requires a tenant and a query", () => {
    expect(buildSearchRequest("", "hello", "10")).toEqual({ ok: false, error: "Choose a tenant" });
    expect(buildSearchRequest("t1", "   ", "10")).toEqual({ ok: false, error: "Type a query" });
  });

  it("bounds k to 1..50, defaulting to 10", () => {
    expect(buildSearchRequest("t1", " hello ", "")).toEqual({ ok: true, value: { tenant_id: "t1", query: "hello", k: 10 } });
    expect(buildSearchRequest("t1", "q", "1")).toMatchObject({ ok: true, value: { k: 1 } });
    expect(buildSearchRequest("t1", "q", "50")).toMatchObject({ ok: true, value: { k: 50 } });
    for (const bad of ["0", "51", "2.5", "x", "-3"]) expect(buildSearchRequest("t1", "q", bad).ok).toBe(false);
  });

  it("rejects an over-long query", () => {
    expect(buildSearchRequest("t1", "x".repeat(2001), "10").ok).toBe(false);
  });
});

describe("buildRedriveRequest", () => {
  it("defaults max to 50, caps it at 200 and drops a blank error code", () => {
    expect(buildRedriveRequest("", "")).toEqual({ ok: true, value: { max: 50 } });
    expect(buildRedriveRequest(" E_FETCH ", "200")).toEqual({ ok: true, value: { error_code: "E_FETCH", max: 200 } });
    expect(buildRedriveRequest("", "201").ok).toBe(false);
    expect(buildRedriveRequest("", "0").ok).toBe(false);
  });
});

describe("summarizeAction", () => {
  it("reads a dry run", () => {
    const s = summarizeAction({
      dry_run: true,
      count: 2,
      messages: [{ article_id: "a1" }, { article_id: "a2" }],
      not_found: ["zz"],
    });
    expect(s).toMatchObject({ dryRun: true, count: 2, articleIds: ["a1", "a2"], notFound: ["zz"], queued: null, dlq: null });
  });

  it("reads a real call and a redrive", () => {
    const s = summarizeAction({
      dry_run: false,
      count: 3,
      queued: 3,
      not_found: ["m1"],
      error_code: null,
      dlq: { messages: 4, moved: true, task_handle: "h" },
    });
    expect(s).toMatchObject({ dryRun: false, queued: 3, notFound: ["m1"], dlq: { messages: 4, moved: true } });
  });

  it("reads only not_found (no other key lists unknown ids)", () => {
    const legacy = { dry_run: true, count: 0, missing: ["m1"] } as unknown as ActionResponse;
    expect(summarizeAction(legacy).notFound).toEqual([]);
  });

  it("knows when there is nothing to do", () => {
    expect(previewHasWork({ dry_run: true, count: 0 })).toBe(false);
    expect(previewHasWork({ dry_run: true, count: 1 })).toBe(true);
    expect(previewHasWork({ dry_run: true, count: 0, dlq: { messages: 3, moved: false } })).toBe(true);
    expect(previewHasWork(null)).toBe(false);
  });
});

describe("misc", () => {
  it("orders statuses by pipeline, unknown last", () => {
    expect(orderStatuses(["FAILED_PERMANENT", "ZED", "CLAIMED", "STAGED"])).toEqual(["CLAIMED", "STAGED", "FAILED_PERMANENT", "ZED"]);
    expect(statusOptions([], "EMBEDDING")).toEqual(["EMBEDDING", "FAILED_RETRYABLE", "FAILED_PERMANENT"]);
  });

  it("reads queue depths and unavailable queues", () => {
    expect(queueDepth({ visible: 1, in_flight: 2 })).toEqual({ visible: 1, in_flight: 2 });
    expect(queueDepth({ error: "unavailable" })).toBeNull();
    expect(queueDepth(undefined)).toBeNull();
  });

  it("lists distinct tenants", () => {
    expect(distinctTenants([{ tenant_id: "b" }, { tenant_id: "a" }, { tenant_id: "b" }, { tenant_id: "" }])).toEqual(["a", "b"]);
  });

  it("truncates snippets and formats scores", () => {
    expect(truncate("a  b\nc")).toBe("a b c");
    const long = truncate("x".repeat(500));
    expect(long.length).toBe(400);
    expect(long.endsWith("…")).toBe(true);
    expect(formatScore(0.032512)).toBe("0.0325");
    expect(formatScore(null)).toBe("-");
  });
});
