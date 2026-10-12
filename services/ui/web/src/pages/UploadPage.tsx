import { type DragEvent, type KeyboardEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "../api";
import { ErrorBanner, Loading } from "../components/common";
import { defaultBatchName, formatBytes, percent } from "../format";
import type { SourceAccess, SourcesResponse } from "../types";
import {
  ACCEPT,
  type BatchResult,
  contentTypeFor,
  type FileState,
  runBatch,
  validateFile,
} from "../upload";

interface Entry {
  id: string;
  file: File;
  title: string;
  externalId: string;
  error: string | null; // validation: the file is not sent
  state: FileState | null; // null until the batch starts
}

let nextId = 0;

function fileKey(f: File): string {
  return `${f.name}\u0000${f.size}\u0000${f.lastModified}`;
}

export function UploadPage() {
  const navigate = useNavigate();
  const [sources, setSources] = useState<SourceAccess[] | null>(null);
  const [sourcesError, setSourcesError] = useState<unknown>(null);
  const [source, setSource] = useState("");
  const [name, setName] = useState(() => defaultBatchName());
  const [entries, setEntries] = useState<Entry[]>([]);
  const [dragging, setDragging] = useState(false);
  const [running, setRunning] = useState(false);
  const [processId, setProcessId] = useState<string | null>(null);
  const [result, setResult] = useState<BatchResult | null>(null);
  const [runError, setRunError] = useState<unknown>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const hintId = useId();

  useEffect(() => {
    const ctrl = new AbortController();
    api
      .get<SourcesResponse>("/api/sources", ctrl.signal)
      .then((r) => {
        const writable = r.items.filter((s) => s.access === "write");
        setSources(writable);
        setSource((cur) => cur || writable[0]?.source || "");
      })
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) setSourcesError(err);
      });
    return () => ctrl.abort();
  }, []);

  // Leaving mid-upload abandons the batch; ask first.
  useEffect(() => {
    if (!running) return undefined;
    const warn = (ev: BeforeUnloadEvent) => {
      ev.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [running]);

  const started = running || result !== null || processId !== null;
  const valid = entries.filter((e) => !e.error);

  const addFiles = useCallback((files: FileList | File[]) => {
    setEntries((current) => {
      const seen = new Set(current.map((e) => fileKey(e.file)));
      const added: Entry[] = [];
      for (const file of Array.from(files)) {
        const key = fileKey(file);
        if (seen.has(key)) continue;
        seen.add(key);
        added.push({ id: `f${++nextId}`, file, title: "", externalId: "", error: validateFile(file), state: null });
      }
      return [...current, ...added];
    });
  }, []);

  const update = (id: string, patch: Partial<Entry>) =>
    setEntries((current) => current.map((e) => (e.id === id ? { ...e, ...patch } : e)));

  const openPicker = () => inputRef.current?.click();

  const onDrop = (ev: DragEvent<HTMLDivElement>) => {
    ev.preventDefault();
    setDragging(false);
    if (!started && ev.dataTransfer.files.length > 0) addFiles(ev.dataTransfer.files);
  };

  const onZoneKey = (ev: KeyboardEvent<HTMLDivElement>) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      openPicker();
    }
  };

  const start = async () => {
    if (!source || valid.length === 0 || !name.trim()) return;
    setRunning(true);
    setRunError(null);
    try {
      const res = await runBatch({
        api,
        source,
        name: name.trim(),
        items: valid.map((e) => ({ id: e.id, file: e.file, title: e.title, externalId: e.externalId })),
        onFileState: (id, state) => update(id, { state }),
        onProcess: (p) => setProcessId(p.process_id),
      });
      setResult(res);
      if (res.failed === 0 && !res.closeError) navigate(`/processes/${encodeURIComponent(res.processId)}`);
    } catch (err) {
      setRunError(err);
    } finally {
      setRunning(false);
    }
  };

  const reset = () => {
    setEntries([]);
    setResult(null);
    setProcessId(null);
    setRunError(null);
    setName(defaultBatchName());
  };

  const progress = useMemo(() => {
    const states = entries.map((e) => e.state).filter((s): s is FileState => s !== null);
    return {
      done: states.filter((s) => s.phase === "done").length,
      failed: states.filter((s) => s.phase === "failed").length,
      total: states.length,
    };
  }, [entries]);

  const selected = sources?.find((s) => s.source === source);

  return (
    <section>
      <h1>Upload documents</h1>
      <p className="muted">
        Files are sent straight to storage as one batch (a process), then ingested. Allowed: HTML, text, Markdown,
        PDF and Word (.docx), up to 5 GiB each.
      </p>

      <ErrorBanner error={sourcesError} />

      <div className="card form-grid">
        <div className="field">
          <label htmlFor="source">Source</label>
          {sources === null && !sourcesError ? (
            <Loading label="Loading sources..." />
          ) : sources && sources.length === 0 ? (
            <p className="alert alert-warn">You can't write to any source.</p>
          ) : (
            <select id="source" value={source} onChange={(e) => setSource(e.target.value)} disabled={started}>
              {(sources ?? []).map((s) => (
                <option key={s.source} value={s.source}>
                  {s.source} (tenant {s.tenant_id})
                </option>
              ))}
            </select>
          )}
          {selected && <span className="hint">Tenant: {selected.tenant_id}</span>}
        </div>
        <div className="field">
          <label htmlFor="batch-name">Batch name</label>
          <input
            id="batch-name"
            type="text"
            value={name}
            maxLength={200}
            required
            disabled={started}
            onChange={(e) => setName(e.target.value)}
          />
          {!name.trim() && <span className="field-error">Enter a batch name.</span>}
        </div>
      </div>

      {!started && (
        <div
          className={`dropzone${dragging ? " dragging" : ""}`}
          role="button"
          tabIndex={0}
          aria-describedby={hintId}
          onClick={openPicker}
          onKeyDown={onZoneKey}
          onDragEnter={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragOver={(e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <strong>Drop files here</strong>
          <span id={hintId}>or press Enter / click to choose files ({ACCEPT.replaceAll(",", " ")})</span>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={ACCEPT}
            hidden
            aria-label="Choose files"
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
      )}

      {entries.length > 0 && (
        <ul className="file-list" aria-label="Files">
          {entries.map((e) => (
            <FileRow key={e.id} entry={e} locked={started} onChange={update} onRemove={(id) => setEntries((c) => c.filter((x) => x.id !== id))} />
          ))}
        </ul>
      )}

      <div className="sr-only" aria-live="polite" role="status">
        {progress.total > 0 && `${progress.done} of ${progress.total} files uploaded${progress.failed ? `, ${progress.failed} failed` : ""}.`}
      </div>

      <ErrorBanner error={runError} />

      {result && (
        <div className={`alert ${result.failed || result.closeError ? "alert-warn" : "alert-ok"}`} role="status">
          {result.succeeded} uploaded, {result.failed} failed.{" "}
          {result.closeError ? `Closing the batch failed: ${result.closeError}. ` : "The batch is closed. "}
          <Link to={`/processes/${encodeURIComponent(result.processId)}`}>Track the batch</Link>
        </div>
      )}
      {running && processId && (
        <p className="muted">
          Batch <code>{processId}</code> is uploading. Keep this page open until it finishes.
        </p>
      )}

      <div className="actions">
        {!result ? (
          <button
            type="button"
            className="primary"
            disabled={running || started || !source || valid.length === 0 || !name.trim()}
            onClick={() => void start()}
          >
            {running ? "Uploading..." : `Start upload${valid.length ? ` (${valid.length} file${valid.length === 1 ? "" : "s"})` : ""}`}
          </button>
        ) : (
          <button type="button" onClick={reset}>
            Start another batch
          </button>
        )}
        {!started && entries.length > 0 && (
          <button type="button" onClick={() => setEntries([])}>
            Clear files
          </button>
        )}
      </div>
    </section>
  );
}

interface FileRowProps {
  entry: Entry;
  locked: boolean;
  onChange: (id: string, patch: Partial<Entry>) => void;
  onRemove: (id: string) => void;
}

function FileRow({ entry, locked, onChange, onRemove }: FileRowProps) {
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

function FileProgress({ state, name }: { state: FileState | null; name: string }) {
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
