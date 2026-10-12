// The browser's only API: same-origin /api/* (the BFF behind CloudFront).
//
// POSTs carry:
// - X-Requested-With: entingestion-ui (the BFF's CSRF guard);
// - x-amz-content-sha256: the hex SHA-256 of the exact body bytes (CloudFront's Lambda OAC
//   signs the origin request and requires it for POST bodies);
// - Idempotency-Key: one UUID per logical call, reused across its retries.
// 429/502/503 and network errors are retried with exponential backoff (at most 3 retries).

export const AUTH_EXPIRED_MESSAGE = "Session expired: reload the page to sign in";
export const MAX_RETRIES = 3;
const RETRY_STATUSES = new Set([429, 502, 503]);

export interface ErrorBody {
  message?: string;
  errors?: unknown;
  [key: string]: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: ErrorBody | null;

  constructor(status: number, body: ErrorBody | null, fallback?: string) {
    super(describeError(status, body, fallback));
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }

  get errors(): unknown {
    return this.body?.errors;
  }
}

export class AuthError extends ApiError {
  constructor(body: ErrorBody | null = null) {
    super(401, body, AUTH_EXPIRED_MESSAGE);
    this.name = "AuthError";
    this.message = AUTH_EXPIRED_MESSAGE;
  }
}

function describeErrors(errors: unknown): string {
  if (!Array.isArray(errors)) return typeof errors === "string" ? errors : "";
  return errors
    .map((e) => {
      if (typeof e === "string") return e;
      if (e && typeof e === "object") {
        const o = e as { loc?: unknown; msg?: unknown; message?: unknown };
        const where = Array.isArray(o.loc) ? `${o.loc.join(".")}: ` : "";
        const text = typeof o.msg === "string" ? o.msg : typeof o.message === "string" ? o.message : JSON.stringify(e);
        return where + text;
      }
      return String(e);
    })
    .join("; ");
}

function describeError(status: number, body: ErrorBody | null, fallback?: string): string {
  const parts: string[] = [];
  if (body?.message) parts.push(String(body.message));
  const details = body?.errors !== undefined ? describeErrors(body.errors) : "";
  if (details) parts.push(details);
  if (parts.length === 0) parts.push(fallback ?? `request failed (HTTP ${status})`);
  return parts.join(": ");
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return toHex(new Uint8Array(digest));
}

export function toHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

export interface ApiClientOptions {
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  baseDelayMs?: number;
  newKey?: () => string;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function readBody(resp: Response): Promise<unknown> {
  const text = await resp.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { message: text.slice(0, 500) };
  }
}

function asErrorBody(body: unknown): ErrorBody | null {
  return body && typeof body === "object" && !Array.isArray(body) ? (body as ErrorBody) : null;
}

function retryAfterMs(resp: Response): number | null {
  const value = resp.headers.get("Retry-After");
  if (!value) return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? Math.min(seconds * 1000, 30_000) : null;
}

export class ApiClient {
  private readonly fetchImpl: typeof fetch;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly baseDelayMs: number;
  private readonly newKey: () => string;

  constructor(options: ApiClientOptions = {}) {
    // Bind: calling an unbound window.fetch throws "Illegal invocation".
    this.fetchImpl = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.sleep = options.sleep ?? defaultSleep;
    this.baseDelayMs = options.baseDelayMs ?? 500;
    this.newKey = options.newKey ?? (() => crypto.randomUUID());
  }

  get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>("GET", path, undefined, signal);
  }

  post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>("POST", path, body ?? {}, signal);
  }

  private async request<T>(method: "GET" | "POST", path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    if (!path.startsWith("/api/")) throw new Error(`refusing non-API path ${path}`);
    const headers: Record<string, string> = { Accept: "application/json" };
    let payload: string | undefined;
    if (method === "POST") {
      payload = JSON.stringify(body);
      headers["Content-Type"] = "application/json";
      headers["X-Requested-With"] = "entingestion-ui";
      headers["x-amz-content-sha256"] = await sha256Hex(payload);
      headers["Idempotency-Key"] = this.newKey(); // stable across this call's retries
    }

    for (let attempt = 0; ; attempt++) {
      let resp: Response;
      try {
        resp = await this.fetchImpl(path, {
          method,
          headers,
          body: payload,
          credentials: "same-origin",
          cache: "no-store",
          signal,
        });
      } catch (err) {
        if (signal?.aborted || attempt >= MAX_RETRIES) throw err;
        await this.sleep(this.backoff(attempt));
        continue;
      }
      if (resp.ok) {
        if (resp.status === 204) return undefined as T;
        return (await readBody(resp)) as T;
      }
      const errorBody = asErrorBody(await readBody(resp));
      if (resp.status === 401) throw new AuthError(errorBody);
      if (RETRY_STATUSES.has(resp.status) && attempt < MAX_RETRIES) {
        await this.sleep(retryAfterMs(resp) ?? this.backoff(attempt));
        continue;
      }
      throw new ApiError(resp.status, errorBody);
    }
  }

  private backoff(attempt: number): number {
    const exp = this.baseDelayMs * 2 ** attempt;
    return exp + Math.random() * this.baseDelayMs;
  }
}

export const api = new ApiClient();

/** One path segment, encoded. Ids are [A-Za-z0-9_-] anyway; this keeps odd input harmless. */
export function seg(value: string): string {
  return encodeURIComponent(value);
}

export function query(params: Record<string, string | number | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
