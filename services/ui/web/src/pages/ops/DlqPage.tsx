import { useState } from "react";
import { Link } from "react-router";
import { api } from "../../api";
import { ConfirmAction } from "../../components/ConfirmAction";
import { ErrorBanner, Loading } from "../../components/common";
import { useOverview } from "../../hooks/useOverview";
import { buildRedriveRequest, DEFAULT_REDRIVE, MAX_REDRIVE, QUEUES, queueDepth } from "../../ops";
import type { ActionResponse } from "../../types";

export function DlqPage() {
  const { overview, error, loading, reload } = useOverview(null);
  const [errorCode, setErrorCode] = useState("");
  const [maxText, setMaxText] = useState(String(DEFAULT_REDRIVE));
  const req = buildRedriveRequest(errorCode, maxText);
  const failed = overview?.statuses;

  const redrive = (dryRun: boolean) => {
    if (!req.ok) return Promise.reject(new Error(req.error));
    return api.post<ActionResponse>("/api/ops/dlq/redrive", { ...req.value, dry_run: dryRun });
  };

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Dead-letter queues</h1>
          <p className="muted small">Redrive re-queues failed articles, resuming where their staged output is intact.</p>
        </div>
        <button type="button" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      <ErrorBanner error={error} />
      {!overview && !error && <Loading />}

      {overview && (
        <ul className="ops-cards">
          {QUEUES.filter((q) => q.dlq).map((q) => {
            const raw = overview.queues[q.key];
            const depth = queueDepth(raw);
            return (
              <li key={q.key} className={`card ops-card tone-${!depth ? "warn" : depth.visible > 0 ? "bad" : "neutral"}`}>
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
              </li>
            );
          })}
          {failed && (
            <li className="card ops-card tone-neutral">
              <div className="ops-card-head">
                <strong>Failed articles</strong>
              </div>
              <dl className="stats">
                <div className="stat">
                  <dt>Retryable</dt>
                  <dd className="ops-count">
                    <Link to="/ops/articles?status=FAILED_RETRYABLE">{failed.FAILED_RETRYABLE?.count ?? 0}</Link>
                  </dd>
                </div>
                <div className="stat">
                  <dt>Permanent</dt>
                  <dd className="ops-count">
                    <Link to="/ops/articles?status=FAILED_PERMANENT">{failed.FAILED_PERMANENT?.count ?? 0}</Link>
                  </dd>
                </div>
              </dl>
            </li>
          )}
        </ul>
      )}

      <form className="card" onSubmit={(e) => e.preventDefault()} noValidate>
        <h2>Redrive</h2>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="dlq-code">Error code (optional)</label>
            <input id="dlq-code" type="text" value={errorCode} onChange={(e) => setErrorCode(e.target.value)} autoComplete="off" />
            <span className="hint">
              Only failed articles with this code. Without one, the ingest DLQ's messages are moved back to the queue too.
            </span>
          </div>
          <div className="field">
            <label htmlFor="dlq-max">Max articles</label>
            <input
              id="dlq-max"
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_REDRIVE}
              value={maxText}
              onChange={(e) => setMaxText(e.target.value)}
              aria-invalid={!req.ok}
              aria-describedby="dlq-max-hint"
            />
            <span id="dlq-max-hint" className={req.ok ? "hint" : "field-error"}>
              {req.ok ? `1 to ${MAX_REDRIVE}` : req.error}
            </span>
          </div>
        </div>
        <div className="actions">
          <ConfirmAction
            label="Preview redrive"
            title="Redrive failed articles"
            buttonClassName="primary"
            confirmLabel="Confirm redrive"
            disabled={!req.ok}
            run={redrive}
            onDone={reload}
          />
        </div>
      </form>
    </section>
  );
}
