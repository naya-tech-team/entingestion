import { useEffect } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { query } from "../api";
import { ErrorBanner, Loading, StatusBadge } from "../components/common";
import { formatTime, shortPrincipal } from "../format";
import { usePagedList } from "../hooks/usePagedList";
import { useSources } from "../hooks/useSources";
import type { Process } from "../types";

const PAGE_SIZE = 50;

export function ProcessesPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const source = params.get("source") ?? "";
  const { sources, error: sourcesError } = useSources();
  const list = usePagedList<Process>(
    (token) => (source ? `/api/processes${query({ source, limit: PAGE_SIZE, next_token: token })}` : null),
    [source],
  );
  const { items, nextToken, loading } = list;
  const error = list.error ?? sourcesError;

  // The list needs a source: default to the first one the caller can read.
  useEffect(() => {
    if (!params.get("source") && sources?.[0]) setParams({ source: sources[0].source }, { replace: true });
    // Once the sources arrive.
  }, [sources]);

  return (
    <section>
      <div className="page-head">
        <h1>Processes</h1>
        <div className="field inline">
          <label htmlFor="proc-source">Source</label>
          <select
            id="proc-source"
            value={source}
            onChange={(e) => setParams({ source: e.target.value })}
            disabled={!sources || sources.length === 0}
          >
            {!source && <option value="">Choose a source</option>}
            {(sources ?? []).map((s) => (
              <option key={s.source} value={s.source}>
                {s.source} (tenant {s.tenant_id}, {s.access})
              </option>
            ))}
          </select>
        </div>
        <button type="button" onClick={list.reload} disabled={!source || loading}>
          Refresh
        </button>
      </div>

      <ErrorBanner error={error} />
      {sources && sources.length === 0 && <p className="alert alert-warn">You can't read any source.</p>}

      {items.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Status</th>
                <th scope="col">Settled</th>
                <th scope="col" className="num">
                  Tickets
                </th>
                <th scope="col" className="num">
                  Registered
                </th>
                <th scope="col" className="num">
                  Rejected
                </th>
                <th scope="col">Created</th>
                <th scope="col">By</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <tr
                  key={p.process_id}
                  className="clickable"
                  onClick={(ev) => {
                    if (!(ev.target instanceof HTMLAnchorElement)) navigate(`/processes/${encodeURIComponent(p.process_id)}`);
                  }}
                >
                  <td>
                    <Link to={`/processes/${encodeURIComponent(p.process_id)}`}>{p.name || p.process_id}</Link>
                  </td>
                  <td>
                    <StatusBadge status={p.status} />
                  </td>
                  <td>{p.settled ? "yes" : "no"}</td>
                  <td className="num">{p.tickets_issued}</td>
                  <td className="num">{p.registered_count}</td>
                  <td className="num">{p.rejected_count}</td>
                  <td>{formatTime(p.created_at)}</td>
                  <td title={p.created_by}>{shortPrincipal(p.created_by)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !loading && source && !error && <p className="muted">No processes for this source yet.</p>
      )}

      {loading && <Loading />}
      {nextToken && !loading && (
        <div className="actions">
          <button type="button" onClick={list.loadMore}>
            Load more
          </button>
        </div>
      )}
    </section>
  );
}
