// The dry-run-then-confirm state machine behind ConfirmAction, kept free of React so it can be
// unit-tested.
//
//   idle --start(run)--> previewing --run(true) ok--> preview --confirm()--> confirming --> done
//                                   \--run(true) fails--> error         \--run(false) fails--> error
//
// - start() always sends the dry run first; nothing is sent with dryRun=false until confirm().
// - confirm() reuses the run function the preview was made with, so the real call has the same
//   parameters as the preview, and works only once per preview (a double click sends one call).
// - cancel() sends nothing. It closes the flow, except while the real call is in flight (it
//   can't be taken back, and its result is shown). A late dry-run answer after cancel is dropped.

export type ConfirmPhase = "idle" | "previewing" | "preview" | "confirming" | "done" | "error";

export interface ConfirmState<R> {
  phase: ConfirmPhase;
  preview: R | null;
  result: R | null;
  error: unknown;
  /** Which call failed, in phase "error". */
  failed: "preview" | "confirm" | null;
}

export type RunAction<R> = (dryRun: boolean) => Promise<R>;

export interface ConfirmFlowOptions<R> {
  onChange?: (state: ConfirmState<R>) => void;
  /** Whether a preview may be confirmed (default: always). */
  canConfirm?: (preview: R) => boolean;
}

export function idleState<R>(): ConfirmState<R> {
  return { phase: "idle", preview: null, result: null, error: null, failed: null };
}

export class ConfirmFlow<R> {
  private current: ConfirmState<R> = idleState<R>();
  private run: RunAction<R> | null = null;
  private generation = 0;
  private readonly onChange: (state: ConfirmState<R>) => void;
  private readonly canConfirmPreview: (preview: R) => boolean;

  constructor(options: ConfirmFlowOptions<R> = {}) {
    this.onChange = options.onChange ?? (() => undefined);
    this.canConfirmPreview = options.canConfirm ?? (() => true);
  }

  get state(): ConfirmState<R> {
    return this.current;
  }

  get busy(): boolean {
    return this.current.phase === "previewing" || this.current.phase === "confirming";
  }

  get canConfirm(): boolean {
    return this.current.phase === "preview" && this.current.preview !== null && this.canConfirmPreview(this.current.preview);
  }

  get canCancel(): boolean {
    return this.current.phase !== "confirming";
  }

  /** Opens the flow and sends the dry run. Ignored while a call is in flight. */
  async start(run: RunAction<R>): Promise<void> {
    if (this.busy) return;
    const gen = ++this.generation;
    this.run = run;
    this.set({ phase: "previewing", preview: null, result: null, error: null, failed: null });
    try {
      const preview = await run(true);
      if (gen !== this.generation) return;
      this.set({ phase: "preview", preview, result: null, error: null, failed: null });
    } catch (error) {
      if (gen !== this.generation) return;
      this.set({ phase: "error", preview: null, result: null, error, failed: "preview" });
    }
  }

  /** Sends the real call (dryRun=false). Only from a confirmable preview. */
  async confirm(): Promise<void> {
    const run = this.run;
    if (!this.canConfirm || !run) return;
    const gen = ++this.generation;
    const preview = this.current.preview;
    this.set({ phase: "confirming", preview, result: null, error: null, failed: null });
    try {
      const result = await run(false);
      if (gen !== this.generation) return;
      this.set({ phase: "done", preview, result, error: null, failed: null });
    } catch (error) {
      if (gen !== this.generation) return;
      this.set({ phase: "error", preview, result: null, error, failed: "confirm" });
    }
  }

  /** Closes the flow without sending anything. Returns false while the real call is in flight. */
  cancel(): boolean {
    if (!this.canCancel) return false;
    this.generation++;
    this.run = null;
    if (this.current.phase !== "idle") this.set(idleState<R>());
    return true;
  }

  private set(state: ConfirmState<R>): void {
    this.current = state;
    this.onChange(state);
  }
}
