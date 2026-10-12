import { describe, expect, it } from "vitest";
import { formatBytes, formatDuration, metricsSummary, shortPrincipal } from "./format";
import { articleActive, orderSteps, processFinished, processProgress, statusTone } from "./status";
import type { Process, ProcessSummary } from "./types";

function summary(status: ProcessSummary["status"], counts = { indexed: 0, skipped: 0, failed: 0 }): ProcessSummary {
  return {
    process_id: "p1",
    status,
    counts,
    producer: { status: "CLOSED", expected_count: 3, registered_count: 3, rejected_count: 0 },
    failures_by_error_code: {},
  };
}

function proc(settled: boolean, registered = 3): Process {
  return {
    process_id: "p1",
    name: "n",
    source: "s",
    status: settled ? "CLOSED" : "OPEN",
    expected_count: 3,
    registered_count: registered,
    rejected_count: 0,
    tickets_issued: 3,
    created_by: "x",
    created_at: "2026-01-01T00:00:00Z",
    settled,
  };
}

describe("processFinished (polling stop condition)", () => {
  it("keeps polling until ingestion is complete and the producer has settled", () => {
    expect(processFinished(null, null)).toBe(false);
    expect(processFinished(null, proc(true))).toBe(false); // summary 404 early on
    expect(processFinished(summary("IN_PROGRESS"), proc(true))).toBe(false);
    expect(processFinished(summary("COMPLETE"), proc(false))).toBe(false);
    expect(processFinished(summary("COMPLETE"), proc(true))).toBe(true);
    expect(processFinished(summary("COMPLETE_WITH_ERRORS"), proc(true))).toBe(true);
    expect(processFinished(summary("COMPLETE"), null)).toBe(true); // producer unavailable
  });
});

describe("articleActive", () => {
  it.each([
    ["RUNNING", true],
    ["STAGED", true],
    ["FAILED_RETRYABLE", true],
    ["EMBEDDING", true],
    ["CLAIMED", true],
    [undefined, true],
    ["INDEXED", false],
    ["SKIPPED_UNCHANGED", false],
    ["SKIPPED", false],
    ["FAILED_PERMANENT", false],
    ["DELETED", false],
  ])("%s -> %s", (status, active) => {
    expect(articleActive(status)).toBe(active);
  });
});

describe("orderSteps", () => {
  it("orders by pipeline position, then by sort key, with unknown steps last", () => {
    const steps = [
      { step: "INDEX", sk: "RUN#1#STEP#08_INDEX" },
      { step: "MYSTERY", sk: "RUN#1#STEP#99_MYSTERY" },
      { step: "FETCH", sk: "RUN#1#STEP#02_FETCH" },
      { step: "EXTRACT", sk: "RUN#1#STEP#03_EXTRACT#2" },
      { step: "OCR", sk: "RUN#1#STEP#03_OCR" },
      { step: "EXTRACT", sk: "RUN#1#STEP#03_EXTRACT#1" },
      { step: "FINALIZE", sk: "RUN#1#STEP#09_FINALIZE" },
      { step: "AAA", sk: "RUN#1#STEP#00_AAA" },
      { step: "CLAIM", sk: "RUN#1#STEP#01_CLAIM" },
    ];
    expect(orderSteps(steps).map((s) => s.sk)).toEqual([
      "RUN#1#STEP#01_CLAIM",
      "RUN#1#STEP#02_FETCH",
      "RUN#1#STEP#03_EXTRACT#1",
      "RUN#1#STEP#03_EXTRACT#2",
      "RUN#1#STEP#03_OCR",
      "RUN#1#STEP#08_INDEX",
      "RUN#1#STEP#09_FINALIZE",
      "RUN#1#STEP#00_AAA",
      "RUN#1#STEP#99_MYSTERY",
    ]);
    expect(steps[0]!.step).toBe("INDEX"); // input untouched
  });
});

describe("processProgress and tones", () => {
  it("counts indexed + skipped + failed out of registered", () => {
    expect(processProgress(summary("IN_PROGRESS", { indexed: 1, skipped: 1, failed: 0 }), proc(false, 4))).toEqual({
      done: 2,
      total: 4,
    });
    expect(processProgress(null, null)).toEqual({ done: 0, total: 0 });
  });

  it("maps statuses to badge tones", () => {
    expect(statusTone("INDEXED")).toBe("ok");
    expect(statusTone("SKIPPED_UNCHANGED")).toBe("warn");
    expect(statusTone("FAILED_PERMANENT")).toBe("bad");
    expect(statusTone("REJECTED")).toBe("bad");
    expect(statusTone("EMBEDDING")).toBe("info");
  });
});

describe("format", () => {
  it("formats sizes, durations, principals and metrics", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GiB");
    expect(formatDuration(850)).toBe("850 ms");
    expect(formatDuration(12_400)).toBe("12 s");
    expect(formatDuration(125_000)).toBe("2 min 5 s");
    expect(formatDuration(null)).toBe("-");
    expect(shortPrincipal("arn:aws:sts::123456789012:assumed-role/ui-dev-bff/ui-dev-bff")).toBe("ui-dev-bff");
    expect(shortPrincipal("arn:aws:iam::123456789012:user/alice")).toBe("alice");
    expect(metricsSummary({ chunks: 12, tokens: 3400, ratio: 0.12345, nested: { a: 1 } })).toBe(
      "chunks=12, tokens=3400, ratio=0.12",
    );
  });
});
