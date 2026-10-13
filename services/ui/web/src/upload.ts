// Upload a batch of files as one process, as the Python client does
// (services/producer/client/producer_client/client.py):
//   create process -> per file: hash (streaming SHA-256), ticket, PUT to S3 -> close.
// The process is closed with expected_count = tickets issued, even when some files failed, so
// it settles: a ticket that never got its object expires and counts as an outcome.

import { createSHA256 } from "hash-wasm";
import { errorMessage, seg } from "./api";
import { formatBytes } from "./format";
import { Sha256 } from "./sha256";
import type { Process, UploadTicket } from "./types";

export const DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export const CONTENT_TYPES: Readonly<Record<string, string>> = {
  ".html": "text/html",
  ".htm": "text/html",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".pdf": "application/pdf",
  ".docx": DOCX_TYPE,
};

/** For <input accept>: the allowed extensions. */
export const ACCEPT = Object.keys(CONTENT_TYPES).join(",");
/**
 * UI default: an early check so a file that can't go up is flagged before the batch starts.
 * The producer enforces the real limit (its MAX_FILE_BYTES setting) when it issues the ticket;
 * this matches its default, S3's single-PUT limit.
 */
export const UI_MAX_FILE_BYTES = 5 * 1024 ** 3;
export const HASH_SLICE_BYTES = 8 * 1024 * 1024;
export const CONCURRENCY = 3;
export const PUT_ATTEMPTS = 3;
const MAX_FILENAME = 255;

export function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot > 0 ? filename.slice(dot).toLowerCase() : "";
}

export function contentTypeFor(filename: string): string | null {
  return CONTENT_TYPES[extensionOf(filename)] ?? null;
}

/** Why a file can't be uploaded, or null when it can. */
export function validateFile(file: { name: string; size: number }): string | null {
  if (!contentTypeFor(file.name)) {
    return "Unsupported type: use .html, .htm, .txt, .md, .markdown, .pdf or .docx";
  }
  if (file.size <= 0) return "The file is empty";
  if (file.size > UI_MAX_FILE_BYTES) return `The file is larger than ${formatBytes(UI_MAX_FILE_BYTES)}`;
  if (file.name.length > MAX_FILENAME) return "The file name is longer than 255 characters";
  if (/[/\\\u0000-\u001f\u007f]/.test(file.name)) return "The file name has a path separator or control character";
  return null;
}

// --- the file queue (the upload page's list) ----------------------------------------------------

/** What the queue needs of a file (a File in the browser; a plain object in tests). */
export type FileLike = Pick<File, "name" | "size" | "lastModified">;

export interface UploadEntry<F extends FileLike = File> {
  id: string;
  file: F;
  title: string;
  externalId: string;
  error: string | null; // validation: the file is not sent
  state: FileState | null; // null until the batch starts
}

function fileKey(f: FileLike): string {
  return `${f.name}\u0000${f.size}\u0000${f.lastModified}`;
}

/** Appends the files not already queued (same name, size and modification time), validated. */
export function addEntries<F extends FileLike>(
  current: readonly UploadEntry<F>[],
  files: Iterable<F>,
  newId: () => string,
): UploadEntry<F>[] {
  const seen = new Set(current.map((e) => fileKey(e.file)));
  const added: UploadEntry<F>[] = [];
  for (const file of files) {
    const key = fileKey(file);
    if (seen.has(key)) continue;
    seen.add(key);
    added.push({ id: newId(), file, title: "", externalId: "", error: validateFile(file), state: null });
  }
  return [...current, ...added];
}

/** Files done and failed, out of those the batch started. */
export function queueProgress(entries: readonly { state: FileState | null }[]): { done: number; failed: number; total: number } {
  const states = entries.map((e) => e.state).filter((s): s is FileState => s !== null);
  return {
    done: states.filter((s) => s.phase === "done").length,
    failed: states.filter((s) => s.phase === "failed").length,
    total: states.length,
  };
}

// --- hashing ----------------------------------------------------------------------------------

export interface Hasher {
  update(data: Uint8Array): unknown;
  digest(): string;
}

/** hash-wasm when WebAssembly may run; the pure TypeScript implementation otherwise. */
export async function createHasher(): Promise<Hasher> {
  try {
    const h = await createSHA256();
    h.init();
    return { update: (d) => h.update(d), digest: () => h.digest("hex") };
  } catch {
    return new Sha256();
  }
}

export async function sha256File(
  file: Blob,
  onProgress?: (fraction: number) => void,
  hasherFactory: () => Promise<Hasher> = createHasher,
): Promise<string> {
  const hasher = await hasherFactory();
  onProgress?.(0);
  for (let offset = 0; offset < file.size; offset += HASH_SLICE_BYTES) {
    const end = Math.min(offset + HASH_SLICE_BYTES, file.size);
    hasher.update(new Uint8Array(await file.slice(offset, end).arrayBuffer()));
    onProgress?.(end / file.size);
  }
  return hasher.digest();
}

// --- PUT --------------------------------------------------------------------------------------

/** The parts of XMLHttpRequest the upload uses (tests pass a fake). */
export interface XhrLike {
  open(method: string, url: string): void;
  setRequestHeader(name: string, value: string): void;
  send(body: Blob): void;
  abort(): void;
  readonly status: number;
  readonly responseText: string;
  upload: { onprogress: ((ev: { loaded: number; total: number; lengthComputable: boolean }) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  ontimeout: (() => void) | null;
  onabort: (() => void) | null;
}

export type XhrFactory = () => XhrLike;

const defaultXhr: XhrFactory = () => new XMLHttpRequest() as unknown as XhrLike;

// Headers the browser sets itself and refuses from scripts. The presigned URL signs
// Content-Length; the browser sends the Blob's exact size, which is what was signed.
const BROWSER_CONTROLLED = new Set(["content-length", "host", "connection"]);

export class PutError extends Error {
  readonly status: number;
  readonly retryable: boolean;
  readonly expired: boolean;

  constructor(status: number, detail: string) {
    const expired = status === 403 && /request has expired/i.test(detail);
    super(
      status === 0
        ? "Upload failed: network error"
        : expired
          ? "Upload URL expired"
          : `Upload failed: HTTP ${status}${s3Message(detail)}`,
    );
    this.name = "PutError";
    this.status = status;
    this.expired = expired;
    this.retryable = status === 0 || status >= 500;
  }
}

function s3Message(xml: string): string {
  const m = /<Message>([^<]*)<\/Message>/.exec(xml);
  return m?.[1] ? ` (${m[1]})` : "";
}

/** The headers the PUT sends: exactly the ticket's, minus those only the browser may set. */
export function putHeaders(ticket: Pick<UploadTicket, "headers">): [string, string][] {
  return Object.entries(ticket.headers).filter(([name]) => !BROWSER_CONTROLLED.has(name.toLowerCase()));
}

export function putFile(
  ticket: UploadTicket,
  file: Blob,
  onProgress: (fraction: number) => void,
  xhrFactory: XhrFactory = defaultXhr,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = xhrFactory();
    xhr.open(ticket.method || "PUT", ticket.url);
    for (const [name, value] of putHeaders(ticket)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (ev) => {
      const total = ev.lengthComputable && ev.total > 0 ? ev.total : file.size;
      onProgress(total > 0 ? Math.min(1, ev.loaded / total) : 0);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve();
      } else {
        reject(new PutError(xhr.status, xhr.responseText || ""));
      }
    };
    xhr.onerror = () => reject(new PutError(0, ""));
    xhr.ontimeout = () => reject(new PutError(0, ""));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(file);
  });
}

// --- the batch --------------------------------------------------------------------------------

export interface UploadItem {
  id: string;
  file: File;
  title?: string;
  externalId?: string;
}

export type FileState =
  | { phase: "queued" }
  | { phase: "hashing"; progress: number }
  | { phase: "requesting" }
  | { phase: "uploading"; progress: number; attempt: number }
  | { phase: "done"; articleId: string; uploadId: string }
  | { phase: "failed"; message: string };

export interface Poster {
  post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T>;
}

export interface BatchOptions {
  api: Poster;
  source: string;
  name: string;
  items: UploadItem[];
  onFileState: (id: string, state: FileState) => void;
  onProcess?: (process: Process) => void;
  concurrency?: number;
  xhrFactory?: XhrFactory;
  hasherFactory?: () => Promise<Hasher>;
  sleep?: (ms: number) => Promise<void>;
  signal?: AbortSignal;
}

export interface BatchResult {
  processId: string;
  ticketsIssued: number;
  succeeded: number;
  failed: number;
  closed: Process | null;
  closeError: string | null;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export async function runBatch(opts: BatchOptions): Promise<BatchResult> {
  const { api, source, name, items, onFileState, signal } = opts;
  const sleep = opts.sleep ?? defaultSleep;
  const concurrency = Math.max(1, opts.concurrency ?? CONCURRENCY);

  const valid: UploadItem[] = [];
  let failed = 0;
  for (const item of items) {
    const problem = validateFile(item.file);
    if (problem) {
      failed++;
      onFileState(item.id, { phase: "failed", message: problem });
    } else {
      valid.push(item);
      onFileState(item.id, { phase: "queued" });
    }
  }

  const process = await api.post<Process>("/api/processes", { name, source }, signal);
  opts.onProcess?.(process);
  const processId = process.process_id;

  let ticketsIssued = 0;
  let succeeded = 0;

  const requestTicket = async (item: UploadItem, sha256: string): Promise<UploadTicket> => {
    const body: Record<string, unknown> = {
      filename: item.file.name,
      content_type: contentTypeFor(item.file.name),
      size_bytes: item.file.size,
      sha256,
    };
    const title = item.title?.trim();
    const externalId = item.externalId?.trim();
    if (title) body.title = title;
    if (externalId) body.external_id = externalId;
    const ticket = await api.post<UploadTicket>(`/api/processes/${seg(processId)}/uploads`, body, signal);
    ticketsIssued++;
    return ticket;
  };

  const uploadOne = async (item: UploadItem): Promise<void> => {
    onFileState(item.id, { phase: "hashing", progress: 0 });
    const sha256 = await sha256File(
      item.file,
      (progress) => onFileState(item.id, { phase: "hashing", progress }),
      opts.hasherFactory,
    );
    onFileState(item.id, { phase: "requesting" });
    let ticket = await requestTicket(item, sha256);
    for (let attempt = 1; ; attempt++) {
      onFileState(item.id, { phase: "uploading", progress: 0, attempt });
      try {
        await putFile(
          ticket,
          item.file,
          (progress) => onFileState(item.id, { phase: "uploading", progress, attempt }),
          opts.xhrFactory,
          signal,
        );
        onFileState(item.id, { phase: "done", articleId: ticket.article_id, uploadId: ticket.upload_id });
        return;
      } catch (err) {
        if (!(err instanceof PutError) || attempt >= PUT_ATTEMPTS || signal?.aborted) throw err;
        if (err.expired) {
          ticket = await requestTicket(item, sha256); // the old ticket expires on its own
        } else if (err.retryable) {
          await sleep(1000 * 2 ** (attempt - 1));
        } else {
          throw err;
        }
      }
    }
  };

  const queue = [...valid];
  const worker = async (): Promise<void> => {
    for (let item = queue.shift(); item; item = queue.shift()) {
      try {
        await uploadOne(item);
        succeeded++;
      } catch (err) {
        failed++;
        onFileState(item.id, { phase: "failed", message: errorMessage(err) });
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, queue.length) }, worker));

  let closed: Process | null = null;
  let closeError: string | null = null;
  try {
    closed = await api.post<Process>(`/api/processes/${seg(processId)}/close`, { expected_count: ticketsIssued });
  } catch (err) {
    closeError = errorMessage(err);
  }
  return { processId, ticketsIssued, succeeded, failed, closed, closeError };
}
