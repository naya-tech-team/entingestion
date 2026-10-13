import { formatBytes } from "../format";
import { contentTypeFor, type UploadEntry } from "../upload";
import { FileProgress } from "./FileProgress";

export interface FileRowProps {
  entry: UploadEntry;
  /** The batch started: the title, external id and the file itself can't change any more. */
  locked: boolean;
  onChange: (id: string, patch: Partial<UploadEntry>) => void;
  onRemove: (id: string) => void;
}

/** One queued file: name, size and type, optional title and external id, and its progress. */
export function FileRow({ entry, locked, onChange, onRemove }: FileRowProps) {
  const { id, file } = entry;
  return (
    <li className={`file-row${entry.error ? " invalid" : ""}`}>
      <div className="file-main">
        <span className="file-name" title={file.name}>
          {file.name}
        </span>
        <span className="muted small">
          {formatBytes(file.size)} · {contentTypeFor(file.name) ?? "unsupported"}
        </span>
      </div>
      {!entry.error && (
        <div className="file-meta">
          <label className="sr-only" htmlFor={`${id}-title`}>
            Title for {file.name}
          </label>
          <input
            id={`${id}-title`}
            type="text"
            placeholder="Title (optional)"
            maxLength={1000}
            value={entry.title}
            disabled={locked}
            onChange={(e) => onChange(id, { title: e.target.value })}
          />
          <label className="sr-only" htmlFor={`${id}-ext`}>
            External id for {file.name}
          </label>
          <input
            id={`${id}-ext`}
            type="text"
            placeholder="External id (optional)"
            maxLength={256}
            value={entry.externalId}
            disabled={locked}
            onChange={(e) => onChange(id, { externalId: e.target.value })}
          />
        </div>
      )}
      <div className="file-status">
        {entry.error ? <span className="field-error">{entry.error}</span> : <FileProgress state={entry.state} name={file.name} />}
      </div>
      {!locked && (
        <button type="button" className="link-button" onClick={() => onRemove(id)} aria-label={`Remove ${file.name}`}>
          Remove
        </button>
      )}
    </li>
  );
}
