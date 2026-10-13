import { describe, expect, it } from "vitest";
import { ApiError } from "../api";
import { appendPage, isNotFound, type LoadState, type Outcome, pollDelay, RequestSlot, runInSlot, settle } from "./request";

/** A promise resolved or rejected from outside, and the signal its request got. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("RequestSlot and runInSlot", () => {
  it("runs one request at a time (single flight): a second load while one runs is skipped", async () => {
    const slot = new RequestSlot();
    const first = deferred<string>();
    let starts = 0;
    const onStart = () => starts++;
    const a = runInSlot(slot, () => first.promise, { onStart });
    expect(slot.busy).toBe(true);
    const b = await runInSlot(slot, () => Promise.resolve("second"), { onStart });
    expect(b).toBeNull();
    expect(starts).toBe(1);
    first.resolve("first");
    expect(await a).toEqual({ ok: true, value: "first" });
    expect(slot.busy).toBe(false);
    // Free again: the next load runs.
    expect(await runInSlot(slot, () => Promise.resolve("third"), { onStart })).toEqual({ ok: true, value: "third" });
    expect(starts).toBe(2);
  });

  it("reports a failure as an outcome, not a throw", async () => {
    const slot = new RequestSlot();
    const err = new ApiError(500, null);
    expect(await runInSlot(slot, () => Promise.reject(err))).toEqual({ ok: false, error: err });
  });

  it("drops the result of an aborted request (unmount, or the key changed)", async () => {
    const slot = new RequestSlot();
    const d = deferred<string>();
    let signal: AbortSignal | undefined;
    const run = runInSlot(slot, (s) => {
      signal = s;
      return d.promise;
    });
    slot.abort();
    expect(signal?.aborted).toBe(true);
    expect(slot.busy).toBe(false);
    d.resolve("late");
    expect(await run).toBeNull();
  });

  it("drops an aborted request that rejects with an AbortError", async () => {
    const slot = new RequestSlot();
    const run = runInSlot(
      slot,
      (s) =>
        new Promise<string>((_, reject) => {
          s.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    slot.abort();
    expect(await run).toBeNull();
  });

  it("replace mode aborts the request in flight; only the newest result applies", async () => {
    const slot = new RequestSlot();
    const old = deferred<string>();
    const signals: AbortSignal[] = [];
    const first = runInSlot(
      slot,
      (s) => {
        signals.push(s);
        return old.promise;
      },
      { replace: true },
    );
    const second = runInSlot(
      slot,
      (s) => {
        signals.push(s);
        return Promise.resolve("new");
      },
      { replace: true },
    );
    expect(signals[0]?.aborted).toBe(true);
    expect(await second).toEqual({ ok: true, value: "new" });
    old.resolve("old");
    expect(await first).toBeNull();
    expect(slot.busy).toBe(false);
  });

  it("a superseded request finishing doesn't free the slot of the newer one", async () => {
    const slot = new RequestSlot();
    const old = deferred<string>();
    const newer = deferred<string>();
    const first = runInSlot(slot, () => old.promise, { replace: true });
    const second = runInSlot(slot, () => newer.promise, { replace: true });
    old.resolve("old");
    expect(await first).toBeNull();
    expect(slot.busy).toBe(true);
    newer.resolve("new");
    expect(await second).toEqual({ ok: true, value: "new" });
  });
});

describe("settle", () => {
  const prev: LoadState<string> = { data: "old", error: null, notFound: false };
  const notFound = new ApiError(404, { message: "not found" });

  it("replaces the data and clears the error on success", () => {
    expect(settle({ data: null, error: new Error("x"), notFound: true }, { ok: true, value: "new" }, true)).toEqual({
      data: "new",
      error: null,
      notFound: false,
    });
  });

  it("keeps the last data when a refresh fails", () => {
    const err = new ApiError(500, null);
    expect(settle(prev, { ok: false, error: err }, false)).toEqual({ data: "old", error: err, notFound: false });
  });

  it("treats a 404 as a state when the caller tolerates it, and as an error otherwise", () => {
    const outcome: Outcome<string> = { ok: false, error: notFound };
    expect(settle(prev, outcome, true)).toEqual({ data: null, error: null, notFound: true });
    expect(settle(prev, outcome, false)).toEqual({ data: "old", error: notFound, notFound: false });
  });

  it("knows a 404", () => {
    expect(isNotFound(notFound)).toBe(true);
    expect(isNotFound(new ApiError(403, null))).toBe(false);
    expect(isNotFound(new Error("404"))).toBe(false);
  });
});

describe("pollDelay", () => {
  const active = (d: { done: boolean }) => d.done;

  it("polls every refreshMs, and not at all without one", () => {
    expect(pollDelay(5000, false, { done: false }, active)).toBe(5000);
    expect(pollDelay(null, false, { done: false }, active)).toBeNull();
    expect(pollDelay(undefined, false, null)).toBeNull();
  });

  it("stops once the session expired", () => {
    expect(pollDelay(5000, true, { done: false }, active)).toBeNull();
    expect(pollDelay(5000, true, null)).toBeNull();
  });

  it("stops when stopWhen holds, and keeps polling until data arrives", () => {
    expect(pollDelay(5000, false, { done: true }, active)).toBeNull();
    expect(pollDelay(5000, false, null, active)).toBe(5000);
  });

  it("re-reads stopWhen: a re-run watch window keeps a final page polling until it ends", () => {
    let watchUntil = Date.now() + 120_000;
    const stopWhen = (d: { done: boolean }) => d.done && watchUntil <= Date.now();
    expect(pollDelay(5000, false, { done: true }, stopWhen)).toBe(5000);
    watchUntil = Date.now() - 1;
    expect(pollDelay(5000, false, { done: true }, stopWhen)).toBeNull();
  });

});

describe("appendPage", () => {
  it("replaces the list with the first page and appends later ones", () => {
    expect(appendPage(["a", "b"], ["c"], null)).toEqual(["c"]);
    expect(appendPage(["a", "b"], ["c"], "tok")).toEqual(["a", "b", "c"]);
    expect(appendPage([], [], null)).toEqual([]);
  });

  it("doesn't mutate the current list", () => {
    const cur = ["a"];
    const next = appendPage(cur, ["b"], "tok");
    expect(cur).toEqual(["a"]);
    expect(next).not.toBe(cur);
  });
});
