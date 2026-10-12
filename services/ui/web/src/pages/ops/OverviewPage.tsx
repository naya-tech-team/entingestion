import { Link } from "react-router";
import { ErrorBanner, Loading, StatusBadge } from "../../components/common";
import { useOverview } from "../../components/useOverview";
import { formatTime } from "../../format";
import { OVERVIEW_REFRESH_MS, orderStatuses, QUEUES, queueDepth, relativeAge } from "../../ops";
import { statusTone } from "../../status";

export function OverviewPage() {
  const { overview, error, loading, updatedAt, authLost, reload } = useOverview(OVERVIEW_REFRESH_MS);
  const now = updatedAt?.getTime() ?? Date.now();
  const statuses = overview ? orderStatuses(Object.keys(overview.statuses)) : [];

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Operations</h1>
          <p className="muted small">
            Articles in progress or failed, and queue depths.
            {overview && <> Generated {formatTime(overview.generated_at)}.</>}
          </p>
        </div>
        <div className="refresh">
          <span className="muted small" aria-live="polite">
            {authLost ? "Paused" : "Auto-refresh every 15 s"}
            {updatedAt && ` · updated ${updatedAt.toLocaleTimeString()}`}
          </span>
          <button type="button" onClick={reload} disabled={loading}>
            Refresh
          </button>
        </div>
      </div>

      <ErrorBanner error={error} />
      {!overview && !error && <Loading />}

      {overview && (
        <>
          <h2>Articles by status</h2>
          {statuses.length === 0 ? (
            <p className="muted">No articles in progress or failed.</p>
          ) : (
            <ul className="ops-cards">
              {statuses.map((status) => {
                const info = overview.statuses[status];
                if (!info) return null;
                const tone = info.count > 0 ? statusTone(status) : "neutral";
                return (
                  <li key={status} className={`card ops-card tone-${tone}`}>
                    <div className="ops-card-head">
                      <StatusBadge status={status} />
                    </div>
                    <Link className="ops-count" to={`/ops/articles?status=${encodeURIComponent(status)}`}>
                      {info.count}
                      <span className="sr-only"> {status} articles</span>
                    </Link>
                    <p className="small muted ops-oldest">
                      {info.oldest_article_id ? (
                        <>
                          oldest:{" "}
                          <Link to={`/articles/${encodeURIComponent(info.oldest_article_id)}`} title={formatTime(info.oldest_at)}>
                            {relativeAge(info.oldest_at, now)}
                          </Link>
                        </>
                      ) : (
                        "oldest: -"
                      )}
                    </p>
                  </li>
                );
              })}
            </ul>
          )}

          <h2>Queues</h2>
          <ul className="ops-cards">
            {QUEUES.map((q) => {
              const raw = overview.queues[q.key];
              const depth = queueDepth(raw);
              const tone = !depth ? "warn" : q.dlq && depth.visible > 0 ? "bad" : "neutral";
              return (
                <li key={q.key} className={`card ops-card tone-${tone}`}>
                  <div className="ops-card-head">
                    <strong>{q.label}</strong>
                  </div>
                  {depth ? (
                    <dl className="stats">
                      <div className="stat">
                        <dt>Visible</dt>
                        <dd className="ops-count">{depth.visible}</dd>
                      </div>
                      <div className="stat">
                        <dt>In flight</dt>
                        <dd className="ops-count">{depth.in_flight}</dd>
                      </div>
                    </dl>
                  ) : (
                    <p className="muted">{raw ? "Unavailable" : "Not reported"}</p>
                  )}
                  {q.dlq && depth && depth.visible > 0 && (
                    <p className="small">
                      <Link to="/ops/dlq">Redrive...</Link>
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
