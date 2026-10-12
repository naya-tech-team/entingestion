import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { api, ApiError, AuthError, query, seg } from "../api";
import { ConfirmAction } from "../components/ConfirmAction";
import { ErrorBanner, Loading, Stat, StatusBadge, useInterval } from "../components/common";
import { formatBytes, formatTime, shortPrincipal } from "../format";
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

function notFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

export function ProcessPage() {
  const { id = "" } = useParams();
  const [process, setProcess] = useState<Process | null>(null);
  const [summary, setSummary] = useState<ProcessSummary | null>(null);
  const [uploads, setUploads] = useState<UploadRow[] | null>(null);
  const [articles, setArticles] = useState<ArticleRow[] | null>(null);
  const [errors, setErrors] = useState<unknown[]>([]);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);
  const [authLost, setAuthLost] = useState(false);
  const inFlight = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (inFlight.current) return; // the previous refresh is still running
    const ctrl = new AbortController();
    inFlight.current = ctrl;
    setLoading(true);
    const [p, s, u, a] = await Promise.allSettled([
      api.get<Process>(`/api/processes/${seg(id)}`, ctrl.signal),
      api.get<ProcessSummary>(`/api/processes/${seg(id)}/summary`, ctrl.signal),
      allUploads(id, ctrl.signal),
      api.get<ProcessArticles>(`/api/processes/${seg(id)}/articles`, ctrl.signal),
    ]);
    if (ctrl.signal.aborted) return;
    const errs: unknown[] = [];
    if (p.status === "fulfilled") setProcess(p.value);
    else errs.push(p.reason);
    // Ingestion has no summary until the first article arrives: not an error.
    if (s.status === "fulfilled") setSummary(s.value);
    else if (!notFound(s.reason)) errs.push(s.reason);
    if (u.status === "fulfilled") setUploads(u.value);
    else errs.push(u.reason);
    if (a.status === "fulfilled") setArticles(a.value.items);
    else errs.push(a.reason);
    const auth = errs.find((e) => e instanceof AuthError);
    setAuthLost(Boolean(auth));
    setErrors(auth ? [auth] : errs);
    setUpdatedAt(new Date());
    setLoading(false);
    inFlight.current = null;
  }, [id]);

  useEffect(() => {
    setProcess(null);
    setSummary(null);
    setUploads(null);
    setArticles(null);
    setErrors([]);
    void load();
    return () => {
      inFlight.current?.abort();
      inFlight.current = null;
    };
  }, [load]);

  const [watchUntil, setWatchUntil] = useState(0);
  // After a re-run, keep polling a while: the summary turns IN_PROGRESS once a run claims an article.
  const finished = processFinished(summary, process) && watchUntil <= Date.now();
  const processMissing = errors.some(notFound) && !process;
  useInterval(() => void load(), finished || authLost || processMissing ? null : POLL_MS);

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
          <button type="button" onClick={() => void load()} disabled={loading}>
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
                <Stat label="Tickets issued" value={process.tickets_issued ?? "-"} />
                <Stat label="Pending" value={process.pending_uploads ?? "-"} />
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
                          void load();
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
