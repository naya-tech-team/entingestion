import { describe, expect, it, vi } from "vitest";
import { ConfirmFlow, type ConfirmState } from "./confirm";
import { previewHasWork } from "./ops";
import type { ActionResponse } from "./types";

const PREVIEW: ActionResponse = { dry_run: true, count: 2, messages: [{ article_id: "a1" }, { article_id: "a2" }], not_found: [] };
const RESULT: ActionResponse = { dry_run: false, count: 2, queued: 2, not_found: [] };

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function fakeRun(preview: ActionResponse = PREVIEW, result: ActionResponse = RESULT) {
  return vi.fn((dryRun: boolean) => Promise.resolve(dryRun ? preview : result));
}

describe("ConfirmFlow", () => {
  it("sends the dry run first and nothing else until confirm", async () => {
    const run = fakeRun();
    const phases: string[] = [];
    const flow = new ConfirmFlow<ActionResponse>({ onChange: (s) => phases.push(s.phase) });
    await flow.start(run);
    expect(run.mock.calls).toEqual([[true]]);
    expect(flow.state.phase).toBe("preview");
    expect(flow.state.preview).toBe(PREVIEW);
    expect(phases).toEqual(["previewing", "preview"]);
  });

  it("confirm sends dry_run false with the same run function, once", async () => {
    const run = fakeRun();
    const flow = new ConfirmFlow<ActionResponse>();
    await flow.start(run);
    const first = flow.confirm();
    const second = flow.confirm(); // a double click
    await Promise.all([first, second]);
    expect(run.mock.calls).toEqual([[true], [false]]);
    expect(flow.state.phase).toBe("done");
    expect(flow.state.result).toBe(RESULT);
    await flow.confirm(); // done: no more calls
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("cancel after the preview sends nothing and closes", async () => {
    const run = fakeRun();
    const flow = new ConfirmFlow<ActionResponse>();
    await flow.start(run);
    expect(flow.cancel()).toBe(true);
    expect(flow.state.phase).toBe("idle");
    await flow.confirm();
    expect(run.mock.calls).toEqual([[true]]);
  });

  it("confirm without a preview does nothing", async () => {
    const flow = new ConfirmFlow<ActionResponse>();
    await flow.confirm();
    expect(flow.state.phase).toBe("idle");
  });

  it("drops a dry-run answer that arrives after cancel", async () => {
    const d = deferred<ActionResponse>();
    const run = vi.fn((_dryRun: boolean) => d.promise);
    const flow = new ConfirmFlow<ActionResponse>();
    const started = flow.start(run);
    expect(flow.state.phase).toBe("previewing");
    expect(flow.canConfirm).toBe(false);
    flow.cancel();
    d.resolve(PREVIEW);
    await started;
    expect(flow.state.phase).toBe("idle");
    await flow.confirm();
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("can't be cancelled while the real call is in flight", async () => {
    const d = deferred<ActionResponse>();
    const run = vi.fn((dryRun: boolean) => (dryRun ? Promise.resolve(PREVIEW) : d.promise));
    const flow = new ConfirmFlow<ActionResponse>();
    await flow.start(run);
    const confirming = flow.confirm();
    expect(flow.state.phase).toBe("confirming");
    expect(flow.cancel()).toBe(false);
    d.resolve(RESULT);
    await confirming;
    expect(flow.state.phase).toBe("done");
  });

  it("reports a failed preview and never sends the real call", async () => {
    const err = new Error("422: at most 100 article_ids");
    const run = vi.fn((_dryRun: boolean) => Promise.reject(err));
    const flow = new ConfirmFlow<ActionResponse>();
    await flow.start(run);
    expect(flow.state).toMatchObject({ phase: "error", failed: "preview", error: err } satisfies Partial<ConfirmState<ActionResponse>>);
    await flow.confirm();
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("reports a failed confirm without retrying it", async () => {
    const err = new Error("boom");
    const run = vi.fn((dryRun: boolean) => (dryRun ? Promise.resolve(PREVIEW) : Promise.reject(err)));
    const flow = new ConfirmFlow<ActionResponse>();
    await flow.start(run);
    await flow.confirm();
    expect(flow.state).toMatchObject({ phase: "error", failed: "confirm", error: err, preview: PREVIEW });
    await flow.confirm();
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("refuses to confirm a preview with nothing to do", async () => {
    const empty: ActionResponse = { dry_run: true, count: 0, messages: [], not_found: ["x"] };
    const run = fakeRun(empty);
    const flow = new ConfirmFlow<ActionResponse>({ canConfirm: previewHasWork });
    await flow.start(run);
    expect(flow.canConfirm).toBe(false);
    await flow.confirm();
    expect(run.mock.calls).toEqual([[true]]);
  });

  it("confirms with the parameters of the preview, not later ones", async () => {
    let force = false;
    const bodies: unknown[] = [];
    const flow = new ConfirmFlow<ActionResponse>();
    const runWith = (f: boolean) => (dryRun: boolean) => {
      bodies.push({ force: f, dry_run: dryRun });
      return Promise.resolve(dryRun ? PREVIEW : RESULT);
    };
    await flow.start(runWith(force));
    force = true; // the page's checkbox changed while the dialog was open
    await flow.confirm();
    expect(force).toBe(true);
    expect(bodies).toEqual([
      { force: false, dry_run: true },
      { force: false, dry_run: false },
    ]);
  });
});
