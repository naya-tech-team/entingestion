import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { Sha256 } from "./sha256";
import type { Process, UploadTicket } from "./types";
import {
  contentTypeFor,
  createHasher,
  DOCX_TYPE,
  type FileState,
  type Poster,
  HASH_SLICE_BYTES,
  MAX_FILE_BYTES,
  putHeaders,
  runBatch,
  sha256File,
  validateFile,
  type XhrLike,
} from "./upload";

// --- content types and validation -------------------------------------------------------------

describe("contentTypeFor", () => {
  it.each([
    ["page.html", "text/html"],
    ["PAGE.HTM", "text/html"],
    ["notes.txt", "text/plain"],
    ["readme.md", "text/markdown"],
    ["readme.markdown", "text/markdown"],
    ["paper.PDF", "application/pdf"],
    ["letter.docx", DOCX_TYPE],
    ["archive.tar.pdf", "application/pdf"],
  ])("%s -> %s", (name, type) => {
    expect(contentTypeFor(name)).toBe(type);
  });

  it.each(["image.png", "old.doc", "noext", ".md", "script.js", "file.html.exe"])("rejects %s", (name) => {
    expect(contentTypeFor(name)).toBeNull();
  });
});

describe("validateFile", () => {
  it("accepts an allowed, non-empty file up to 5 GiB", () => {
    expect(validateFile({ name: "a.pdf", size: 1 })).toBeNull();
    expect(validateFile({ name: "a.pdf", size: MAX_FILE_BYTES })).toBeNull();
  });

  it("rejects other types, empty and oversized files, and bad names", () => {
    expect(validateFile({ name: "a.png", size: 10 })).toMatch(/Unsupported type/);
    expect(validateFile({ name: "a.txt", size: 0 })).toMatch(/empty/);
    expect(validateFile({ name: "a.txt", size: MAX_FILE_BYTES + 1 })).toMatch(/5 GiB/);
    expect(validateFile({ name: `${"x".repeat(252)}.txt`, size: 1 })).toMatch(/255/);
    expect(validateFile({ name: "a\u0007.txt", size: 1 })).toMatch(/control/);
  });
});

// --- hashing ----------------------------------------------------------------------------------

async function webCryptoHex(data: Uint8Array<ArrayBuffer>): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
}

function bytes(n: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i++) out[i] = (i * 31 + 7) & 0xff;
  return out;
}

describe("hashing", () => {
  it("the fallback SHA-256 matches known vectors and WebCrypto at block edges", async () => {
    expect(new Sha256().update(new TextEncoder().encode("abc")).digest()).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    expect(new Sha256().digest()).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    for (const n of [1, 55, 56, 63, 64, 65, 119, 120, 1000]) {
      const data = bytes(n);
      const h = new Sha256();
      h.update(data.subarray(0, 7)).update(data.subarray(7)); // split updates
      expect(h.digest()).toBe(await webCryptoHex(data));
    }
  });

  it("streams a file in slices, reports progress and matches WebCrypto (hash-wasm and fallback)", async () => {
    const data = bytes(HASH_SLICE_BYTES * 2 + 12345);
    const file = new Blob([data]);
    const expected = await webCryptoHex(data);

    const progress: number[] = [];
    expect(await sha256File(file, (p) => progress.push(p))).toBe(expected);
    expect(progress).toEqual([0, HASH_SLICE_BYTES / file.size, (2 * HASH_SLICE_BYTES) / file.size, 1]);

    expect(await sha256File(file, undefined, async () => new Sha256())).toBe(expected);
  });

  it("uses hash-wasm when WebAssembly is available", async () => {
    const h = await createHasher();
    expect(h).not.toBeInstanceOf(Sha256);
  });
});

// --- PUT and the batch ------------------------------------------------------------------------

type Behaviour = "ok" | "500" | "network" | "expired" | "403";

class FakeXhr implements XhrLike {
  method = "";
  url = "";
  headers: [string, string][] = [];
  body: Blob | null = null;
  status = 0;
  responseText = "";
  upload: XhrLike["upload"] = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;

  constructor(
    public behaviour: Behaviour,
    private readonly log: FakeXhr[],
  ) {}

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers.push([name, value]);
  }
  abort() {
    this.onabort?.();
  }
  send(body: Blob) {
    this.body = body;
    this.log.push(this);
    setTimeout(() => {
      const size = body.size;
      switch (this.behaviour) {
        case "ok":
          this.upload.onprogress?.({ loaded: size / 2, total: size, lengthComputable: true });
          this.status = 200;
          this.onload?.();
          break;
        case "500":
          this.status = 500;
          this.responseText = "<Error><Code>InternalError</Code><Message>We encountered an internal error.</Message></Error>";
          this.onload?.();
          break;
        case "expired":
          this.status = 403;
          this.responseText = "<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>";
          this.onload?.();
          break;
        case "403":
          this.status = 403;
          this.responseText = "<Error><Code>SignatureDoesNotMatch</Code><Message>bad signature</Message></Error>";
          this.onload?.();
          break;
        case "network":
          this.onerror?.();
          break;
      }
    }, 0);
  }
}

/** The XHR behaviour per URL: a list consumed in order (the last one repeats). */
function xhrScript(script: Record<string, Behaviour[]>) {
  const log: FakeXhr[] = [];
  const seen: Record<string, number> = {};
  const factory = () => {
    // The URL isn't known until open(); defer choosing the behaviour.
    const proxy = new FakeXhr("ok", log);
    const open = proxy.open.bind(proxy);
    proxy.open = (method: string, url: string) => {
      open(method, url);
      const list = script[url] ?? ["ok"];
      const i = seen[url] ?? 0;
      seen[url] = i + 1;
      proxy.behaviour = list[Math.min(i, list.length - 1)]!;
    };
    return proxy;
  };
  return { factory, log };
}

const TICKET_HEADERS = {
  "Content-Type": "application/pdf",
  "Content-Length": "5",
  "x-amz-checksum-sha256": "base64sum",
  "x-amz-meta-upload-id": "u",
};

function fakeApi(opts: { failTicketFor?: string } = {}) {
  let tickets = 0;
  const posts: { path: string; body: Record<string, unknown> }[] = [];
  const handler = vi.fn(async (path: string, body: unknown): Promise<unknown> => {
    const b = body as Record<string, unknown>;
    posts.push({ path, body: b });
    if (path === "/api/processes") {
      return { process_id: "p1", name: b.name, source: b.source, status: "OPEN" };
    }
    if (path === "/api/processes/p1/uploads") {
      if (b.filename === opts.failTicketFor) throw new ApiError(409, { message: "duplicate external_id" });
      const n = ++tickets;
      const ticket: UploadTicket = {
        upload_id: `u${n}`,
        article_id: `art-${String(b.filename)}`,
        process_id: "p1",
        method: "PUT",
        url: `https://raw.s3.example/${String(b.filename)}?ticket=${n}`,
        headers: { ...TICKET_HEADERS, "Content-Length": String(b.size_bytes) },
        expires_at: "2030-01-01T00:00:00Z",
      };
      return ticket;
    }
    if (path === "/api/processes/p1/close") {
      return { process_id: "p1", status: "CLOSED", expected_count: b.expected_count };
    }
    throw new Error(`unexpected POST ${path}`);
  });
  const api: Poster = { post: <T,>(path: string, body: unknown) => handler(path, body) as Promise<T> };
  return { api, posts };
}

function file(name: string, text = "hello"): File {
  return new File([text], name, { type: "" });
}

function track() {
  const states = new Map<string, FileState[]>();
  const onFileState = (id: string, s: FileState) => {
    states.set(id, [...(states.get(id) ?? []), s]);
  };
  const last = (id: string) => states.get(id)?.at(-1);
  return { states, onFileState, last };
}

const noSleep = async () => {};

describe("putHeaders", () => {
  it("is exactly the ticket's headers minus those only the browser may set", () => {
    expect(putHeaders({ headers: TICKET_HEADERS })).toEqual([
      ["Content-Type", "application/pdf"],
      ["x-amz-checksum-sha256", "base64sum"],
      ["x-amz-meta-upload-id", "u"],
    ]);
  });
});

describe("runBatch", () => {
  it("creates the process, gets a ticket per file, PUTs with the ticket's headers and closes", async () => {
    const { api, posts } = fakeApi();
    const { factory, log } = xhrScript({});
    const t = track();
    const a = file("a.pdf", "aaaaa");
    const b = file("b.md", "# b");
    const bad = file("c.exe", "x");
    const processes: Process[] = [];

    const result = await runBatch({
      api,
      source: "bruno",
      name: "batch 1",
      items: [
        { id: "a", file: a, title: "  Title A ", externalId: "ext-a" },
        { id: "b", file: b },
        { id: "c", file: bad },
      ],
      onFileState: t.onFileState,
      onProcess: (p) => processes.push(p),
      xhrFactory: factory,
      sleep: noSleep,
    });

    expect(posts[0]).toEqual({ path: "/api/processes", body: { name: "batch 1", source: "bruno" } });
    expect(processes.map((p) => p.process_id)).toEqual(["p1"]);

    const ticketBodies = posts.filter((p) => p.path.endsWith("/uploads")).map((p) => p.body);
    expect(ticketBodies).toHaveLength(2);
    const ta = ticketBodies.find((x) => x.filename === "a.pdf")!;
    expect(ta).toEqual({
      filename: "a.pdf",
      content_type: "application/pdf",
      size_bytes: 5,
      sha256: await webCryptoHex(new TextEncoder().encode("aaaaa")),
      title: "Title A",
      external_id: "ext-a",
    });
    const tb = ticketBodies.find((x) => x.filename === "b.md")!;
    expect(tb).toEqual({ filename: "b.md", content_type: "text/markdown", size_bytes: 3, sha256: expect.any(String) });

    expect(log).toHaveLength(2);
    const putA = log.find((x) => x.url.startsWith("https://raw.s3.example/a.pdf"))!;
    expect(putA.method).toBe("PUT");
    expect(putA.body).toBe(a);
    expect(putA.headers).toEqual([
      ["Content-Type", "application/pdf"],
      ["x-amz-checksum-sha256", "base64sum"],
      ["x-amz-meta-upload-id", "u"],
    ]);

    expect(posts.at(-1)).toEqual({ path: "/api/processes/p1/close", body: { expected_count: 2 } });
    expect(result).toMatchObject({ processId: "p1", ticketsIssued: 2, succeeded: 2, failed: 1, closeError: null });

    expect(t.last("a")).toEqual({ phase: "done", articleId: "art-a.pdf", uploadId: expect.any(String) });
    expect(t.last("c")).toEqual({ phase: "failed", message: expect.stringMatching(/Unsupported type/) });
    const phases = t.states.get("a")!.map((s) => s.phase);
    expect(phases).toEqual(["queued", "hashing", "hashing", "hashing", "requesting", "uploading", "uploading", "uploading", "done"]);
    expect(t.states.get("a")).toContainEqual({ phase: "uploading", progress: 0.5, attempt: 1 });
  });

  it("retries a PUT after a 5xx or network error with the same ticket", async () => {
    const { api, posts } = fakeApi();
    const { factory, log } = xhrScript({ "https://raw.s3.example/a.pdf?ticket=1": ["500", "network", "ok"] });
    const sleep = vi.fn(async (_ms: number) => {});
    const t = track();
    const result = await runBatch({
      api,
      source: "s",
      name: "n",
      items: [{ id: "a", file: file("a.pdf") }],
      onFileState: t.onFileState,
      xhrFactory: factory,
      sleep,
    });
    expect(log).toHaveLength(3);
    expect(sleep.mock.calls.map((c) => c[0])).toEqual([1000, 2000]);
    expect(result).toMatchObject({ ticketsIssued: 1, succeeded: 1, failed: 0 });
    expect(posts.at(-1)!.body).toEqual({ expected_count: 1 });
    expect(t.states.get("a")).toContainEqual({ phase: "uploading", progress: 0, attempt: 3 });
  });

  it("gets a fresh ticket when the URL expired, and counts both tickets", async () => {
    const { api, posts } = fakeApi();
    const { factory, log } = xhrScript({ "https://raw.s3.example/a.pdf?ticket=1": ["expired"] });
    const t = track();
    const result = await runBatch({
      api,
      source: "s",
      name: "n",
      items: [{ id: "a", file: file("a.pdf") }],
      onFileState: t.onFileState,
      xhrFactory: factory,
      sleep: noSleep,
    });
    expect(log.map((x) => x.url)).toEqual([
      "https://raw.s3.example/a.pdf?ticket=1",
      "https://raw.s3.example/a.pdf?ticket=2",
    ]);
    expect(result).toMatchObject({ ticketsIssued: 2, succeeded: 1 });
    expect(posts.at(-1)).toEqual({ path: "/api/processes/p1/close", body: { expected_count: 2 } });
  });

  it("closes the process even when uploads fail, counting only issued tickets", async () => {
    const { api, posts } = fakeApi({ failTicketFor: "dup.txt" });
    const { factory, log } = xhrScript({
      "https://raw.s3.example/a.pdf?ticket=1": ["500"],
      "https://raw.s3.example/b.pdf?ticket=2": ["403"],
    });
    const t = track();
    const result = await runBatch({
      api,
      source: "s",
      name: "n",
      items: [
        { id: "a", file: file("a.pdf") },
        { id: "b", file: file("b.pdf") },
        { id: "dup", file: file("dup.txt") },
      ],
      onFileState: t.onFileState,
      xhrFactory: factory,
      sleep: noSleep,
      concurrency: 1,
    });
    // a: 3 attempts (5xx), b: 1 attempt (a 403 that isn't an expiry won't succeed on retry).
    expect(log).toHaveLength(4);
    expect(t.last("a")).toEqual({ phase: "failed", message: "Upload failed: HTTP 500 (We encountered an internal error.)" });
    expect(t.last("b")).toEqual({ phase: "failed", message: "Upload failed: HTTP 403 (bad signature)" });
    expect(t.last("dup")).toEqual({ phase: "failed", message: "duplicate external_id" });
    expect(posts.at(-1)).toEqual({ path: "/api/processes/p1/close", body: { expected_count: 2 } });
    expect(result).toMatchObject({ ticketsIssued: 2, succeeded: 0, failed: 3, closeError: null });
  });

  it("reports a failed close without throwing", async () => {
    const inner = fakeApi().api;
    const api: Poster = {
      post: async <T,>(path: string, body: unknown): Promise<T> => {
        if (path.endsWith("/close")) throw new ApiError(409, { message: "process p1 is not OPEN" });
        return inner.post<T>(path, body);
      },
    };
    const { factory } = xhrScript({});
    const result = await runBatch({
      api,
      source: "s",
      name: "n",
      items: [{ id: "a", file: file("a.pdf") }],
      onFileState: () => {},
      xhrFactory: factory,
      sleep: noSleep,
    });
    expect(result.closeError).toBe("process p1 is not OPEN");
    expect(result.succeeded).toBe(1);
  });

  it("runs at most `concurrency` files at a time", async () => {
    const { api } = fakeApi();
    const { factory } = xhrScript({});
    const active = new Set<string>();
    let peak = 0;
    const result = await runBatch({
      api,
      source: "s",
      name: "n",
      items: Array.from({ length: 7 }, (_, i) => ({ id: `f${i}`, file: file(`f${i}.txt`) })),
      onFileState: (id, s) => {
        if (s.phase === "hashing") active.add(id);
        if (s.phase === "done" || s.phase === "failed") active.delete(id);
        peak = Math.max(peak, active.size);
      },
      xhrFactory: factory,
      sleep: noSleep,
      concurrency: 3,
    });
    expect(result.succeeded).toBe(7);
    expect(peak).toBe(3);
  });

  it("closes a batch with no valid files with expected_count 0", async () => {
    const { api, posts } = fakeApi();
    const t = track();
    const result = await runBatch({
      api,
      source: "s",
      name: "n",
      items: [{ id: "x", file: file("x.png") }],
      onFileState: t.onFileState,
      sleep: noSleep,
    });
    // The UI never starts an empty batch; if it did, the process closes with 0 and settles.
    expect(posts.map((p) => p.path)).toEqual(["/api/processes", "/api/processes/p1/close"]);
    expect(posts.at(-1)!.body).toEqual({ expected_count: 0 });
    expect(result.failed).toBe(1);
  });
});
