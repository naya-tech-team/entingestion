// A button that previews an action with a dry run and needs an explicit confirm to send it.
// The state machine is ConfirmFlow (../confirm.ts); this renders it as a modal dialog.

import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";
import { ConfirmFlow, type ConfirmState, idleState, type RunAction } from "../confirm";
import { plural, previewHasWork, summarizeAction } from "../ops";
import type { ActionResponse } from "../types";
import { ErrorBanner } from "./common";

const MAX_IDS_SHOWN = 200;

export interface ConfirmActionProps {
  /** The button's text, also the dialog's title unless `title` is given. */
  label: string;
  title?: string;
  /** Sends the action: called with true for the preview, then with false on confirm. */
  run: RunAction<ActionResponse>;
  disabled?: boolean;
  /** Shown above the preview (what the action does). */
  description?: ReactNode;
  confirmLabel?: string;
  /** Called once the real call succeeded. */
  onDone?: (result: ActionResponse) => void;
  buttonClassName?: string;
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function ConfirmAction({
  label,
  title,
  run,
  disabled = false,
  description,
  confirmLabel = "Confirm",
  onDone,
  buttonClassName,
}: ConfirmActionProps) {
  const [state, setState] = useState<ConfirmState<ActionResponse>>(idleState);
  const flowRef = useRef<ConfirmFlow<ActionResponse> | null>(null);
  if (flowRef.current === null) flowRef.current = new ConfirmFlow<ActionResponse>({ onChange: setState, canConfirm: previewHasWork });
  const flow = flowRef.current;
  const dialogRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const onDoneRef = useRef(onDone);
  const titleId = useId();
  const open = state.phase !== "idle";

  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  useEffect(() => {
    if (state.phase === "done" && state.result) onDoneRef.current?.(state.result);
  }, [state.phase, state.result]);

  useEffect(() => {
    if (!open) return undefined;
    dialogRef.current?.focus();
    const trigger = triggerRef.current;
    return () => trigger?.focus();
  }, [open]);

  function close() {
    flow.cancel();
  }

  function onKeyDown(ev: KeyboardEvent<HTMLDivElement>) {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      close();
      return;
    }
    if (ev.key !== "Tab" || !dialogRef.current) return;
    // Keep focus inside the dialog.
    const items = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = items[0];
    const last = items[items.length - 1];
    if (!first || !last) {
      ev.preventDefault();
      return;
    }
    const active = document.activeElement;
    if (ev.shiftKey && (active === first || active === dialogRef.current)) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && active === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={buttonClassName}
        disabled={disabled || open}
        onClick={() => void flow.start(run)}
      >
        {label}
      </button>
      {open && (
        <div className="modal-backdrop">
          <div
            ref={dialogRef}
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            tabIndex={-1}
            onKeyDown={onKeyDown}
          >
            <h2 id={titleId}>{title ?? label}</h2>
            {description && <div className="small muted">{description}</div>}
            <DialogBody state={state} />
            <div className="actions modal-actions">
              {state.phase === "done" || state.phase === "error" ? (
                <button type="button" className="primary" onClick={close}>
                  Close
                </button>
              ) : (
                <>
                  <button type="button" className="primary" onClick={() => void flow.confirm()} disabled={!flow.canConfirm}>
                    {state.phase === "confirming" ? "Sending..." : confirmLabel}
                  </button>
                  <button type="button" onClick={close} disabled={!flow.canCancel}>
                    Cancel
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function DialogBody({ state }: { state: ConfirmState<ActionResponse> }) {
  if (state.phase === "previewing") {
    return (
      <p className="muted" role="status">
        Preparing a preview (dry run, nothing is queued)...
      </p>
    );
  }
  if (state.phase === "error") {
    return (
      <>
        <p className="small">{state.failed === "preview" ? "The preview failed; nothing was sent." : "The action failed."}</p>
        <ErrorBanner error={state.error} />
      </>
    );
  }
  if (state.phase === "done") return <ResultView result={state.result} />;
  return <PreviewView preview={state.preview} />;
}

function IdList({ ids, label }: { ids: string[]; label: string }) {
  if (ids.length === 0) return null;
  const shown = ids.slice(0, MAX_IDS_SHOWN);
  return (
    <>
      <h3>{label}</h3>
      <ul className="id-list" aria-label={label}>
        {shown.map((id) => (
          <li key={id}>
            <code>{id}</code>
          </li>
        ))}
      </ul>
      {ids.length > shown.length && <p className="small muted">and {ids.length - shown.length} more</p>}
    </>
  );
}

function PreviewView({ preview }: { preview: ActionResponse | null }) {
  const s = summarizeAction(preview);
  return (
    <div role="status">
      <p>
        Preview: <strong>{plural(s.count, "article")}</strong> would be queued
        {s.errorCode && (
          <>
            {" "}
            (error code <code>{s.errorCode}</code>)
          </>
        )}
        .
      </p>
      {s.dlq && (
        <p>
          The ingest DLQ holds <strong>{plural(s.dlq.messages, "message")}</strong>
          {s.dlq.messages > 0 ? "; they would be moved back to the ingest queue." : "."}
        </p>
      )}
      <IdList ids={s.articleIds} label="To be queued" />
      <IdList ids={s.notFound} label="Not found (skipped)" />
      {!previewHasWork(preview) && <p className="alert alert-info">Nothing to do.</p>}
    </div>
  );
}

function ResultView({ result }: { result: ActionResponse | null }) {
  const s = summarizeAction(result);
  return (
    <div role="status">
      <p className="alert alert-ok">
        Queued {plural(s.queued ?? s.count, "article")}.
        {s.dlq && (s.dlq.moved ? ` Moving ${plural(s.dlq.messages, "message")} from the ingest DLQ.` : " The DLQ was not moved.")}
      </p>
      <IdList ids={s.notFound} label="Not found (skipped)" />
    </div>
  );
}
