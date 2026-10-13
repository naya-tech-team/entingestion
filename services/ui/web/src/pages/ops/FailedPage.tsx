import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, query } from "../../api";
import { ConfirmAction } from "../../components/ConfirmAction";
import { ErrorBanner, Loading, StatusBadge } from "../../components/common";
import { useOverview } from "../../hooks/useOverview";
import { usePagedList } from "../../hooks/usePagedList";
import { formatTime } from "../../format";
import {
  allSelected,
  checkSelection,
  DEFAULT_FAILED_STATUS,
  MAX_RERUN_IDS,
  someSelected,
  statusOptions,
  toggleAll,
  toggleOne,
} from "../../ops";
import type { ActionResponse, OpsArticle } from "../../types";

const PAGE_SIZE = 50;

function countLabel(count: number | undefined): string {
  return count === undefined ? "" : ` (${count})`;
}

export function FailedPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || DEFAULT_FAILED_STATUS;
  const { overview } = useOverview(null);
  const { items, nextToken, loading, loaded, error, loadMore, reload } = usePagedList<OpsArticle>(
    (token) => `/api/ops/articles${query({ status, limit: PAGE_SIZE, next_token: token })}`,
    [status],
  );
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [force, setForce] = useState(false);
  const selectAllRef = useRef<HTMLInputElement>(null);
  // Another status is another list: drop the selection.
  useEffect(() => setSelected(new Set()), [status]);

  const pageIds = items.map((a) => a.article_id);
  const all = allSelected(selected, pageIds);
  const some = someSelected(selected, pageIds);
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = some;
  }, [some]);

  const check = checkSelection(selected.size);
  const options = statusOptions(Object.keys(overview?.statuses ?? {}), status);
  const ids = [...selected];

  const rerun = (dryRun: boolean) =>
    api.post<ActionResponse>("/api/ops/ingest", { article_ids: ids, force, dry_run: dryRun });

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Failed &amp; stuck articles</h1>
          <p className="muted small">Articles by ingestion status, oldest update first.</p>
        </div>
        <div className="field inline">
          <label htmlFor="ops-status">Status</label>
          <select id="ops-status" value={status} onChange={(e) => setParams({ status: e.target.value })}>
            {options.map((s) => (
              <option key={s} value={s}>
                {s}
                {countLabel(overview?.statuses[s]?.count)}
              </option>
            ))}
          </select>
        </div>
        <button type="button" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      <div className="card toolbar">
        <span className={check.canRun || selected.size === 0 ? "small muted" : "small field-error"} aria-live="polite">
          {check.message}
        </span>
        <label className="checkbox">
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
          Force (re-run even if the content is unchanged)
        </label>
        <ConfirmAction
          label="Re-run selected"
          title={`Re-run ${selected.size} selected article${selected.size === 1 ? "" : "s"}`}
          buttonClassName="primary"
          disabled={!check.canRun}
          description={
            <p>
              Each article resumes after its last intact step
              {force ? "; Force re-runs it even if its content is unchanged" : ""}. At most {MAX_RERUN_IDS} articles at a
              time.
            </p>
          }
          run={rerun}
          onDone={() => {
            setSelected(new Set());
            reload();
          }}
        />
      </div>

      <ErrorBanner error={error} />

      {items.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col" className="check-col">
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    checked={all}
                    onChange={() => setSelected((cur) => toggleAll(cur, pageIds))}
                    aria-label="Select all articles on this page"
                  />
                </th>
                <th scope="col">Article</th>
                <th scope="col">Status</th>
                <th scope="col">Stage</th>
                <th scope="col">Error code</th>
                <th scope="col">Error</th>
                <th scope="col">Process</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.article_id} className={selected.has(a.article_id) ? "selected" : undefined}>
                  <td className="check-col">
                    <input
                      type="checkbox"
                      checked={selected.has(a.article_id)}
                      onChange={() => setSelected((cur) => toggleOne(cur, a.article_id))}
                      aria-label={`Select ${a.article_id}`}
                    />
                  </td>
                  <td>
                    <Link to={`/articles/${encodeURIComponent(a.article_id)}`}>
                      <code>{a.article_id}</code>
                    </Link>
                  </td>
                  <td>
                    <StatusBadge status={a.status} />
                  </td>
                  <td>{a.stage ?? "-"}</td>
                  <td>{a.error_code ? <code>{a.error_code}</code> : "-"}</td>
                  <td className="truncate" title={a.error_msg ?? undefined}>
                    {a.error_msg ?? "-"}
                  </td>
                  <td>
                    {a.process_id ? (
                      <Link to={`/processes/${encodeURIComponent(a.process_id)}`}>
                        <code>{a.process_id}</code>
                      </Link>
                    ) : (
                      "-"
                    )}
                  </td>
                  <td>{formatTime(a.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        loaded && !loading && !error && <p className="muted">No {status} articles.</p>
      )}

      {loading && <Loading />}
      {nextToken && !loading && (
        <div className="actions">
          <button type="button" onClick={loadMore}>
            Load more
          </button>
        </div>
      )}
    </section>
  );
}
