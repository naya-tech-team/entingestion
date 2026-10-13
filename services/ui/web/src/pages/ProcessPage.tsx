import { useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { api, AuthError, query, seg } from "../api";
import { ConfirmAction } from "../components/ConfirmAction";
import { ErrorBanner, Loading, Stat, StatusBadge } from "../components/common";
import { formatBytes, formatTime, shortPrincipal } from "../format";
import { isNotFound } from "../hooks/request";
import { useLoader } from "../hooks/useApi";
import { RERUN_WATCH_MS } from "../ops";
import { POLL_MS, processFinished, processProgress } from "../status";
import type { ActionResponse, ArticleRow, Process, ProcessArticles, ProcessSummary, UploadPage, UploadRow } from "../types";

const UPLOAD_PAGE = 500;
const MAX_UPLOAD_PAGES = 20;

async function allUploads(id: string, signal: AbortSignal): Promise<UploadRow[]> {
  const rows: UploadRow[] = [];
  let token: string | null = null;
  for (let page = 0; page < MAX_UPLOAD_PAGES; page++) {
    const res: UploadPage = await api.get<UploadPage>(
      `/api/processes/${seg(id)}/uploads${query({ limit: UPLOAD_PAGE, next_token: token })}`,
      signal,
    );
    rows.push(...res.items);
    token = res.next_token;
    if (!token) break;
  }
  return rows;
}

interface ProcessView {
  process: Process | null;
  summary: ProcessSummary | null;
  uploads: UploadRow[] | null;
  articles: ArticleRow[] | null;
  errors: unknown[];
  authLost: boolean;
}

/**
 * The page's four reads, in parallel. A part that fails keeps its last value and adds its
 * error; ingestion has no summary until the first article arrives, so its 404 is not an error.
 * An expired session reports only the 401.
 */
async function loadProcess(id: string, signal: AbortSignal, prev: ProcessView | null): Promise<ProcessView> {
  const [p, s, u, a] = await Promise.allSettled([
    api.get<Process>(`/api/processes/${seg(id)}`, signal),
    api.get<ProcessSummary>(`/api/processes/${seg(id)}/summary`, signal),
    allUploads(id, signal),
    api.get<ProcessArticles>(`/api/processes/${seg(id)}/articles`, signal),
  ]);
  const errs: unknown[] = [];
  const keep = <T,>(r: PromiseSettledResult<T>, last: T | null, tolerate404 = false): T | null => {
    if (r.status === "fulfilled") return r.value;
    if (!(tolerate404 && isNotFound(r.reason))) errs.push(r.reason);
    return last;
  };
  const process = keep(p, prev?.process ?? null);
  const summary = keep(s, prev?.summary ?? null, true);
  const uploads = keep(u, prev?.uploads ?? null);
  const articlesPage = keep(a, null);
  const articles = articlesPage ? articlesPage.items : (prev?.articles ?? null);
  const auth = errs.find((e) => e instanceof AuthError);
  return { process, summary, uploads, articles, errors: auth ? [auth] : errs, authLost: Boolean(auth) };
}

function processMissingIn(view: ProcessView | null): boolean {
  return Boolean(view && view.errors.some(isNotFound) && !view.process);
}

export function ProcessPage() {
  const { id = "" } = useParams();
  const [watchUntil, setWatchUntil] = useState(0);
  // Poll until ingestion is complete and the producer has settled, the session expired or the
  // process doesn't exist. After a re-run, keep polling a while: the summary turns IN_PROGRESS
  // once a run claims an article.
  const { data, loading, updatedAt, reload } = useLoader<ProcessView>(id, (signal, prev) => loadProcess(id, signal, prev), {
    refreshMs: POLL_MS,
    stopWhen: (v) => (processFinished(v.summary, v.process) && watchUntil <= Date.now()) || v.authLost || processMissingIn(v),
  });
  const process = data?.process ?? null;
  const summary = data?.summary ?? null;
  const uploads = data?.uploads ?? null;
  const articles = data?.articles ?? null;
  const errors = data?.errors ?? [];
  const authLost = data?.authLost ?? false;
  const finished = processFinished(summary, process) && watchUntil <= Date.now();
  const processMissing = processMissingIn(data);

  const filenames = useMemo(() => {
    const byArticle = new Map<string, string>();
    for (const u of uploads ?? []) {
      // Uploads are oldest first: the newest upload of an article wins.
      if (u.status !== "REJECTED" && u.status !== "EXPIRED") byArticle.set(u.article_id, u.filename);
      else if (!byArticle.has(u.article_id)) byArticle.set(u.article_id, u.filename);
    }
    return byArticle;
  }, [uploads]);

  const { done, total } = processProgress(summary, process);

  return (
    <section>
      <div className="page-head">
        <div>
          <p className="crumbs">
            <Link to={process ? `/processes?source=${encodeURIComponent(process.source)}` : "/processes"}>Processes</Link> /
          </p>
          <h1>{process?.name ?? id}</h1>
          <p className="muted small">
            <code>{id}</code>
            {process && (
              <>
                {" "}
                · source {process.source} · created {formatTime(process.created_at)} by{" "}
                <span title={process.created_by}>{shortPrincipal(process.created_by)}</span>
              </>
            )}
          </p>
        </div>
        <div className="refresh">
          <span className="muted small" aria-live="polite">
            {finished ? "Finished" : authLost ? "Paused" : "Auto-refresh every 5 s"}
            {updatedAt && ` · updated ${updatedAt.toLocaleTimeString()}`}
          </span>
          <button type="button" onClick={reload} disabled={loading}>
            Refresh
          </button>
        </div>
      </div>

      {processMissing ? (
        <div className="alert alert-error" role="alert">
          Process {id} was not found.
        </div>
      ) : (
        errors.map((e, i) => <ErrorBanner key={i} error={e} />)
      )}

      {!process && !processMissing && errors.length === 0 && <Loading />}

      {process && (
        <>
          <div className="card">
            <div className="progress-head">
              <h2>Ingestion progress</h2>
              {summary ? <StatusBadge status={summary.status} /> : <span className="muted small">Not started yet</span>}
            </div>
            <progress className="wide" max={Math.max(total, 1)} value={done} aria-label="Ingestion progress" />
            <p className="small" aria-live="polite">
              {done} of {total} registered articles finished
              {summary && ` (${summary.counts.indexed} indexed, ${summary.counts.skipped} skipped, ${summary.counts.failed} failed)`}
            </p>
          </div>

          <div className="grid-2">
            <div className="card">
              <h2>Upload batch</h2>
              <dl className="stats">
                <Stat label="Status" value={<StatusBadge status={process.status} />} />
                <Stat label="Settled" value={process.settled ? "yes" : "no"} />
                <Stat label="Expected" value={process.expected_count ?? "-"} />
                <Stat label="Tickets issued" value={process.tickets_issued} />
                <Stat label="Pending" value={process.pending_uploads} />
                <Stat label="Registered" value={process.registered_count} />
                <Stat label="Rejected" value={process.rejected_count} />
                <Stat label="Superseded" value={process.superseded_count ?? 0} />
                <Stat label="Expired" value={process.expired_count ?? 0} />
                <Stat label="Closed" value={formatTime(process.closed_at)} />
              </dl>
            </div>
            <div className="card">
              <h2>Ingestion</h2>
              {summary ? (
                <>
                  <dl className="stats">
                    <Stat label="Status" value={<StatusBadge status={summary.status} />} />
                    <Stat label="Indexed" value={summary.counts.indexed} />
                    <Stat label="Skipped" value={summary.counts.skipped} />
                    <Stat label="Failed" value={summary.counts.failed} />
                  </dl>
                  {Object.keys(summary.failures_by_error_code).length > 0 && (
                    <>
                      <h3>Failures by error code</h3>
                      <ul className="plain">
                        {Object.entries(summary.failures_by_error_code).map(([code, n]) => (
                          <li key={code}>
                            <code>{code}</code>: {n}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                  {summary.counts.failed > 0 && (
                    <div className="actions">
                      <ConfirmAction
                        label="Re-run failed articles"
                        title={`Re-run the failed articles of ${process.name || id}`}
                        description={<p>Each failed article resumes after its last intact step.</p>}
                        run={(dryRun) =>
                          api.post<ActionResponse>(`/api/ops/processes/${seg(id)}/ingest`, {
                            only_failed: true,
                            dry_run: dryRun,
                          })
                        }
                        onDone={() => {
                          setWatchUntil(Date.now() + RERUN_WATCH_MS);
                          reload();
                        }}
                      />
                    </div>
                  )}
                </>
              ) : (
                <p className="muted">Ingestion hasn't reported on this batch yet.</p>
              )}
            </div>
          </div>
        </>
      )}

      <h2>Articles</h2>
      {articles === null ? (
        !processMissing && <Loading />
      ) : articles.length === 0 ? (
        <p className="muted">No articles yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Article</th>
                <th scope="col">File</th>
                <th scope="col">Status</th>
                <th scope="col">Stage</th>
                <th scope="col" className="num">
                  Chunks
                </th>
                <th scope="col">Error</th>
                <th scope="col">Indexed</th>
              </tr>
            </thead>
            <tbody>
              {articles.map((a) => (
                <tr key={a.article_id}>
                  <td>
                    <Link to={`/articles/${encodeURIComponent(a.article_id)}`}>
                      <code>{a.article_id}</code>
                    </Link>
                  </td>
                  <td className="truncate" title={filenames.get(a.article_id)}>
                    {filenames.get(a.article_id) ?? "-"}
                  </td>
                  <td>
                    <StatusBadge status={a.status} />
                  </td>
                  <td>{a.stage ?? "-"}</td>
                  <td className="num">{a.chunk_count ?? "-"}</td>
                  <td title={a.error_msg ?? undefined}>{a.error_code ? <code>{a.error_code}</code> : "-"}</td>
                  <td>{formatTime(a.indexed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>Uploads</h2>
      {uploads === null ? (
        !processMissing && <Loading />
      ) : uploads.length === 0 ? (
        <p className="muted">No uploads in this batch.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col" className="num">
                  Size
                </th>
                <th scope="col">Status</th>
                <th scope="col">Reason</th>
                <th scope="col">Article</th>
                <th scope="col">Created</th>
              </tr>
            </thead>
            <tbody>
              {uploads.map((u) => (
                <tr key={u.upload_id}>
                  <td className="truncate" title={u.filename}>
                    {u.filename}
                  </td>
                  <td className="num">{formatBytes(u.size_bytes)}</td>
                  <td>
                    <StatusBadge status={u.status} />
                  </td>
                  <td>{u.reason ?? "-"}</td>
                  <td>
                    <Link to={`/articles/${encodeURIComponent(u.article_id)}`}>
                      <code>{u.article_id}</code>
                    </Link>
                  </td>
                  <td>{formatTime(u.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
