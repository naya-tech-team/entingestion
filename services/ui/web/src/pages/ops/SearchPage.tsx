import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api } from "../../api";
import { ErrorBanner, Loading } from "../../components/common";
import { buildSearchRequest, DEFAULT_K, distinctTenants, formatScore, MAX_K, MAX_QUERY_CHARS, MIN_K, truncate } from "../../ops";
import type { SearchResponse, SourcesResponse } from "../../types";

function rank(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "-";
}

export function SearchPage() {
  const [tenants, setTenants] = useState<string[] | null>(null);
  const [tenant, setTenant] = useState("");
  const [queryText, setQueryText] = useState("");
  const [kText, setKText] = useState(String(DEFAULT_K));
  const [formError, setFormError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    api
      .get<SourcesResponse>("/api/sources", ctrl.signal)
      .then((r) => {
        const list = distinctTenants(r.items);
        setTenants(list);
        setTenant((cur) => cur || list[0] || "");
      })
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) setError(err);
      });
    return () => {
      ctrl.abort();
      inFlight.current?.abort();
    };
  }, []);

  async function onSubmit(ev: FormEvent<HTMLFormElement>) {
    ev.preventDefault();
    const req = buildSearchRequest(tenant, queryText, kText);
    if (!req.ok) {
      setFormError(req.error);
      return;
    }
    setFormError(null);
    inFlight.current?.abort();
    const ctrl = new AbortController();
    inFlight.current = ctrl;
    setLoading(true);
    setError(null);
    try {
      setResponse(await api.post<SearchResponse>("/api/ops/search", req.value, ctrl.signal));
    } catch (err) {
      if (ctrl.signal.aborted) return;
      setError(err);
      setResponse(null);
    }
    setLoading(false);
    inFlight.current = null;
  }

  const results = response?.results ?? [];

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Search</h1>
          <p className="muted small">Hybrid (vector and keyword) search within one tenant.</p>
        </div>
      </div>

      <form className="card" onSubmit={(e) => void onSubmit(e)} noValidate>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="search-tenant">Tenant</label>
            <select
              id="search-tenant"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              disabled={!tenants || tenants.length === 0}
              required
            >
              {!tenant && <option value="">{tenants ? "No tenants" : "Loading..."}</option>}
              {(tenants ?? []).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="search-k">Results (k)</label>
            <input
              id="search-k"
              type="number"
              inputMode="numeric"
              min={MIN_K}
              max={MAX_K}
              value={kText}
              onChange={(e) => setKText(e.target.value)}
            />
            <span className="hint">
              {MIN_K} to {MAX_K}
            </span>
          </div>
        </div>
        <div className="field search-query">
          <label htmlFor="search-query">Query</label>
          <textarea
            id="search-query"
            rows={3}
            maxLength={MAX_QUERY_CHARS}
            value={queryText}
            onChange={(e) => setQueryText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit();
            }}
            required
          />
        </div>
        {formError && (
          <p className="field-error" role="alert">
            {formError}
          </p>
        )}
        <div className="actions">
          <button type="submit" className="primary" disabled={loading}>
            Search
          </button>
        </div>
      </form>

      <ErrorBanner error={error} />
      {loading && <Loading label="Searching..." />}

      {response && !loading && (
        <>
          <p className="muted small" aria-live="polite">
            {results.length} result{results.length === 1 ? "" : "s"} for tenant <code>{response.tenant_id}</code>
            {response.table && (
              <>
                {" "}
                from <code>{response.table}</code>
              </>
            )}
            {response.reranked ? " (reranked)" : ""}
          </p>
          {results.length === 0 ? (
            <p className="muted">No matches.</p>
          ) : (
            <ol className="results">
              {results.map((r) => (
                <li key={r.chunk_id} className="card result">
                  <div className="result-head">
                    <Link to={`/articles/${encodeURIComponent(r.article_id)}`}>
                      <strong>{r.title || r.article_id}</strong>
                    </Link>
                    <span className="small muted result-scores">
                      score {formatScore(r.score)} · vector #{rank(r.vector_rank)} · keyword #{rank(r.keyword_rank)}
                    </span>
                  </div>
                  {r.heading_path && r.heading_path.length > 0 && (
                    <p className="small muted result-path">{r.heading_path.join(" › ")}</p>
                  )}
                  <p className="result-snippet">{truncate(r.text)}</p>
                  <p className="small muted">
                    {r.source ?? "-"}
                    {r.published_at && ` · published ${r.published_at}`}
                    {typeof r.page_start === "number" &&
                      ` · page ${r.page_start}${r.page_end && r.page_end !== r.page_start ? `-${r.page_end}` : ""}`}
                    {" · "}
                    <code>{r.article_id}</code>
                  </p>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
