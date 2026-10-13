import { useState } from "react";
import { Link, useParams } from "react-router";
import { api, seg } from "../api";
import { ConfirmAction } from "../components/ConfirmAction";
import { ErrorBanner, Loading, Stat, StatusBadge } from "../components/common";
import { formatDuration, formatTime, metricsSummary } from "../format";
import { useApi } from "../hooks/useApi";
import { RERUN_WATCH_MS } from "../ops";
import { articleActive, orderSteps, POLL_MS } from "../status";
import type { ActionResponse, ArticleRun, ArticleStatus } from "../types";

function runDuration(run: ArticleRun): string {
  if (!run.started_at || !run.ended_at) return "-";
  const ms = new Date(run.ended_at).getTime() - new Date(run.started_at).getTime();
  return Number.isFinite(ms) && ms >= 0 ? formatDuration(ms) : "-";
}

function text(value: unknown): string {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

export function ArticlePage() {
  const { id = "" } = useParams();
  const [force, setForce] = useState(false);
  const [watchUntil, setWatchUntil] = useState(0);
  // Keep refreshing while the article may still change, and while it hasn't arrived yet (a 404
  // means "not ingested yet"). After a re-run, keep polling a while: the queued run takes a
  // moment to claim the article.
  const {
    data,
    notFound: missing,
    error,
    loading,
    updatedAt,
    polling,
    reload,
  } = useApi<ArticleStatus>(`/api/articles/${seg(id)}/status`, {
    refreshMs: POLL_MS,
    notFoundOk: true,
    stopWhen: (d) => !articleActive(d.latest.status) && watchUntil <= Date.now(),
  });
  const stopped = !polling;

  const latest = data?.latest;
  const currentRun = data?.runs.find((r) => r.run_id === latest?.run_id);
  const model = latest?.embedding_model ?? latest?.model ?? currentRun?.embedding_model ?? null;
  const steps = data ? orderSteps(data.steps) : [];

  return (
    <section>
      <div className="page-head">
        <div>
          <p className="crumbs">
            <Link to="/processes">Processes</Link> /
          </p>
          <h1>
            Article <code>{id}</code>
          </h1>
        </div>
        <div className="refresh">
          <span className="muted small" aria-live="polite">
            {stopped ? "Final status" : "Auto-refresh every 5 s"}
            {updatedAt && ` · updated ${updatedAt.toLocaleTimeString()}`}
          </span>
          <button type="button" onClick={reload} disabled={loading}>
            Refresh
          </button>
        </div>
      </div>

      <ErrorBanner error={error} />
      {missing && (
        <div className="alert alert-info" role="status">
          Not ingested yet. Ingestion starts once the upload is registered; this page refreshes on its own.
        </div>
      )}
      {!data && !missing && !error && <Loading />}

      {data && latest && (
        <>
          <div className="card">
            <div className="progress-head">
              <h2>Latest</h2>
              <StatusBadge status={latest.status} />
            </div>
            <dl className="stats">
              <Stat label="Stage" value={text(latest.stage)} />
              <Stat label="Doc version" value={text(latest.doc_version)} />
              <Stat label="Chunks" value={text(latest.chunk_count)} />
              <Stat label="Model" value={text(model)} />
              <Stat label="Indexed" value={formatTime(latest.indexed_at)} />
              <Stat label="Updated" value={formatTime(latest.updated_at)} />
              <Stat label="Run" value={latest.run_id ? <code>{latest.run_id}</code> : "-"} />
            </dl>
            {(latest.error_code || latest.error_msg) && (
              <div className="alert alert-error">
                {latest.error_class && <strong>{latest.error_class} </strong>}
                {latest.error_code && <code>{latest.error_code}</code>}
                {latest.error_msg && <p className="pre">{latest.error_msg}</p>}
              </div>
            )}
            {data.execution && (
              <p>
                <a href={data.execution} target="_blank" rel="noopener noreferrer">
                  Open the Step Functions execution
                </a>
              </p>
            )}
            <div className="actions rerun">
              <ConfirmAction
                label="Re-run this article"
                title={`Re-run article ${id}`}
                description={
                  <p>
                    The article resumes after its last intact step
                    {force ? "; Force re-runs it even if its content is unchanged" : ""}.
                  </p>
                }
                run={(dryRun) =>
                  api.post<ActionResponse>("/api/ops/ingest", { article_ids: [id], force, dry_run: dryRun })
                }
                onDone={() => {
                  setWatchUntil(Date.now() + RERUN_WATCH_MS);
                  reload();
                }}
              />
              <label className="checkbox">
                <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
                Force
              </label>
            </div>
          </div>

          <h2>Steps</h2>
          {steps.length === 0 ? (
            <p className="muted">No steps recorded for this run yet.</p>
          ) : (
            <ol className="timeline">
              {steps.map((s) => (
                <li key={s.sk} className={`step step-${s.step_status.toLowerCase()}`}>
                  <div className="step-head">
                    <strong>{s.step}</strong>
                    <StatusBadge status={s.step_status} />
                    <span className="muted small">
                      attempt {text(s.attempt)} · {formatDuration(s.duration_ms)}
                    </span>
                  </div>
                  {metricsSummary(s.metrics) && <p className="small muted metrics">{metricsSummary(s.metrics)}</p>}
                  {(s.error_class || s.error_code || s.error_msg) && (
                    <div className="step-error small">
                      {s.error_class && <strong>{s.error_class} </strong>}
                      {s.error_code && <code>{s.error_code}</code>}
                      {s.error_msg && <p className="pre">{s.error_msg}</p>}
                    </div>
                  )}
                </li>
              ))}
            </ol>
          )}

          <h2>Recent runs</h2>
          {data.runs.length === 0 ? (
            <p className="muted">No runs.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Run</th>
                    <th scope="col">Trigger</th>
                    <th scope="col">Claim</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Started</th>
                    <th scope="col">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {data.runs.map((r) => (
                    <tr key={r.run_id}>
                      <td>
                        <code>{r.run_id}</code>
                      </td>
                      <td>{text(r.trigger)}</td>
                      <td>{text(r.claim)}</td>
                      <td>{r.outcome ? <StatusBadge status={r.outcome} /> : "-"}</td>
                      <td>{formatTime(r.started_at)}</td>
                      <td>{runDuration(r)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
