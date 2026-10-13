import { type DragEvent, type KeyboardEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "../api";
import { ErrorBanner, Loading } from "../components/common";
import { FileRow } from "../components/FileRow";
import { defaultBatchName, formatBytes } from "../format";
import { useSources } from "../hooks/useSources";
import { useUploadQueue } from "../hooks/useUploadQueue";
import { ACCEPT, type BatchResult, runBatch, UI_MAX_FILE_BYTES } from "../upload";

export function UploadPage() {
  const navigate = useNavigate();
  const { sources: allSources, error: sourcesError } = useSources();
  const sources = useMemo(() => allSources?.filter((s) => s.access === "write") ?? null, [allSources]);
  const [source, setSource] = useState("");
  const [name, setName] = useState(() => defaultBatchName());
  const { entries, valid, progress, addFiles, update, remove, clear } = useUploadQueue();
  const [dragging, setDragging] = useState(false);
  const [running, setRunning] = useState(false);
  const [processId, setProcessId] = useState<string | null>(null);
  const [result, setResult] = useState<BatchResult | null>(null);
  const [runError, setRunError] = useState<unknown>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const hintId = useId();

  useEffect(() => {
    if (sources) setSource((cur) => cur || sources[0]?.source || "");
  }, [sources]);

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
    clear();
    setResult(null);
    setProcessId(null);
    setRunError(null);
    setName(defaultBatchName());
  };

  const selected = sources?.find((s) => s.source === source);

  return (
    <section>
      <h1>Upload documents</h1>
      <p className="muted">
        Files are sent straight to storage as one batch (a process), then ingested. Allowed: HTML, text, Markdown,
        PDF and Word (.docx), up to {formatBytes(UI_MAX_FILE_BYTES)} each.
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
            <FileRow key={e.id} entry={e} locked={started} onChange={update} onRemove={remove} />
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
          <button type="button" onClick={clear}>
            Clear files
          </button>
        )}
      </div>
    </section>
  );
}
