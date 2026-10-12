import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, AUTH_EXPIRED_MESSAGE, AuthError, MAX_RETRIES, query, sha256Hex } from "./api";

type Call = { url: string; init: RequestInit };

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function client(responses: Array<Response | Error>) {
  const calls: Call[] = [];
  const sleep = vi.fn(async (_ms: number) => {});
  let n = 0;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const r = responses[Math.min(n++, responses.length - 1)]!;
    if (r instanceof Error) throw r;
    return r.clone();
  });
  let k = 0;
  const api = new ApiClient({ fetch: fetchMock as unknown as typeof fetch, sleep, newKey: () => `key-${++k}` });
  return { api, calls, sleep, fetchMock };
}

const header = (c: Call, name: string) => (c.init.headers as Record<string, string>)[name];

describe("sha256Hex", () => {
  it("hashes the UTF-8 bytes of a string", async () => {
    expect(await sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect(await sha256Hex("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  });
});

describe("ApiClient POST", () => {
  it("sends JSON with the CSRF header, the body hash and an idempotency key", async () => {
    const { api, calls } = client([jsonResponse(201, { process_id: "p1" })]);
    const res = await api.post<{ process_id: string }>("/api/processes", { name: "batch 1", source: "bruno" });
    expect(res.process_id).toBe("p1");
    expect(calls).toHaveLength(1);
    const c = calls[0]!;
    expect(c.url).toBe("/api/processes");
    expect(c.init.method).toBe("POST");
    expect(c.init.body).toBe('{"name":"batch 1","source":"bruno"}');
    expect(header(c, "Content-Type")).toBe("application/json");
    expect(header(c, "X-Requested-With")).toBe("entingestion-ui");
    // Same value as `printf '%s' '{"name":"batch 1","source":"bruno"}' | shasum -a 256`.
    expect(header(c, "x-amz-content-sha256")).toBe("0bccaf1facf713dee4a8e9a1d301365d8859a373c1913cff8b398b16a9003694");
    expect(header(c, "x-amz-content-sha256")).toBe(await sha256Hex(c.init.body as string));
    expect(header(c, "Idempotency-Key")).toBe("key-1");
    expect(c.init.credentials).toBe("same-origin");
  });

  it("hashes exactly the bytes it sends, including non-ASCII text", async () => {
    const { api, calls } = client([jsonResponse(200, {})]);
    await api.post("/api/processes/p1/uploads", { filename: "résumé ✓.pdf" });
    const c = calls[0]!;
    expect(header(c, "x-amz-content-sha256")).toBe(await sha256Hex(c.init.body as string));
  });

  it("retries 429 with the same idempotency key and body hash", async () => {
    const { api, calls, sleep } = client([
      jsonResponse(429, { message: "slow down" }, { "Retry-After": "2" }),
      jsonResponse(503, { message: "unavailable" }),
      jsonResponse(200, { ok: true }),
    ]);
    await expect(api.post("/api/processes/p1/close", { expected_count: 2 })).resolves.toEqual({ ok: true });
    expect(calls).toHaveLength(3);
    const keys = calls.map((c) => header(c, "Idempotency-Key"));
    expect(new Set(keys)).toEqual(new Set(["key-1"]));
    const hashes = calls.map((c) => header(c, "x-amz-content-sha256"));
    expect(new Set(hashes).size).toBe(1);
    expect(sleep).toHaveBeenCalledTimes(2);
    expect(sleep.mock.calls[0]![0]).toBe(2000); // Retry-After honoured
  });

  it("uses a new idempotency key for each logical call", async () => {
    const { api, calls } = client([jsonResponse(200, {})]);
    await api.post("/api/processes", { a: 1 });
    await api.post("/api/processes", { a: 1 });
    expect(header(calls[0]!, "Idempotency-Key")).not.toBe(header(calls[1]!, "Idempotency-Key"));
  });

  it("gives up after the retry budget and reports the last error", async () => {
    const { api, calls } = client([jsonResponse(502, { message: "upstream unavailable" })]);
    const err = await api.post("/api/processes", {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(502);
    expect((err as ApiError).message).toBe("upstream unavailable");
    expect(calls).toHaveLength(MAX_RETRIES + 1);
  });

  it("retries network errors", async () => {
    const { api, calls } = client([new TypeError("Failed to fetch"), jsonResponse(200, { ok: 1 })]);
    await expect(api.post("/api/processes", {})).resolves.toEqual({ ok: 1 });
    expect(calls).toHaveLength(2);
    expect(header(calls[0]!, "Idempotency-Key")).toBe(header(calls[1]!, "Idempotency-Key"));
  });
});

describe("ApiClient errors and GET", () => {
  it("does not retry client errors and exposes message and errors", async () => {
    const body = { message: "invalid request", errors: [{ loc: ["body", "size_bytes"], msg: "must be > 0" }] };
    const { api, calls } = client([jsonResponse(400, body)]);
    const err = (await api.post("/api/processes/p/uploads", {}).catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(400);
    expect(err.body).toEqual(body);
    expect(err.errors).toEqual(body.errors);
    expect(err.message).toBe("invalid request: body.size_bytes: must be > 0");
    expect(calls).toHaveLength(1);
  });

  it("turns 401 into an AuthError asking for a reload", async () => {
    const { api } = client([jsonResponse(401, { message: "Unauthorized" })]);
    const err = await api.get("/api/sources").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(AuthError);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as AuthError).message).toBe(AUTH_EXPIRED_MESSAGE);
    expect(AUTH_EXPIRED_MESSAGE).toBe("Session expired: reload the page to sign in");
  });

  it("GETs without the POST-only headers", async () => {
    const { api, calls } = client([jsonResponse(200, { items: [] })]);
    await api.get("/api/sources");
    const c = calls[0]!;
    expect(c.init.method).toBe("GET");
    expect(c.init.body).toBeUndefined();
    expect(header(c, "X-Requested-With")).toBeUndefined();
    expect(header(c, "x-amz-content-sha256")).toBeUndefined();
    expect(header(c, "Idempotency-Key")).toBeUndefined();
  });

  it("only calls same-origin /api paths", async () => {
    const { api, fetchMock } = client([jsonResponse(200, {})]);
    await expect(api.get("https://example.com/api/x")).rejects.toThrow(/non-API path/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("builds query strings without empty values", () => {
    expect(query({ source: "bruno", limit: 50, next_token: null })).toBe("?source=bruno&limit=50");
    expect(query({ next_token: "a b+c" })).toBe("?next_token=a+b%2Bc");
    expect(query({})).toBe("");
  });
});
