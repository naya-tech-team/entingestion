import { Link } from "react-router";
import { percent } from "../format";
import type { FileState } from "../upload";

/** One file's upload phase: ready, checksum and upload progress, done (with its article) or failed. */
export function FileProgress({ state, name }: { state: FileState | null; name: string }) {
  if (!state) return <span className="muted small">Ready</span>;
  switch (state.phase) {
    case "queued":
      return <span className="muted small">Queued</span>;
    case "hashing":
      return (
        <span className="progress-cell">
          <progress max={1} value={state.progress} aria-label={`Checksum of ${name}`} />
          <span className="small">Checksum {percent(state.progress)}</span>
        </span>
      );
    case "requesting":
      return <span className="muted small">Requesting upload URL...</span>;
    case "uploading":
      return (
        <span className="progress-cell">
          <progress max={1} value={state.progress} aria-label={`Upload of ${name}`} />
          <span className="small">
            Uploading {percent(state.progress)}
            {state.attempt > 1 ? ` (attempt ${state.attempt})` : ""}
          </span>
        </span>
      );
    case "done":
      return (
        <span className="small ok-text">
          Uploaded · <Link to={`/articles/${encodeURIComponent(state.articleId)}`}>{state.articleId}</Link>
        </span>
      );
    case "failed":
      return <span className="field-error">{state.message}</span>;
  }
}
