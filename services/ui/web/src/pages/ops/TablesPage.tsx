import { ErrorBanner, Loading, Stat } from "../../components/common";
import { useApi } from "../../hooks/useApi";
import type { RegistryState, TablesResponse, TableStats } from "../../types";

function num(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "-";
}

function writes(reg: RegistryState | null | undefined): string {
  const w = reg?.writes;
  return Array.isArray(w) && w.length > 0 ? w.join(", ") : "-";
}

/** A plain object (the response is rendered defensively: a field may be missing or of another shape). */
function isRecord<T extends object>(value: T | null | undefined): value is T {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function TablesPage() {
  const { data, error, loading, reload } = useApi<TablesResponse>("/api/ops/tables");

  const registry = isRecord(data?.registry) ? data.registry : null;
  const tenants = isRecord(data?.tenants) ? Object.entries(data.tenants).sort(([a], [b]) => a.localeCompare(b)) : [];
  const tables = isRecord(data?.tables) ? Object.entries(data.tables).sort(([a], [b]) => a.localeCompare(b)) : [];
  const profiles = isRecord(data?.profiles) ? Object.entries(data.profiles) : [];
  const liveTables = new Set([registry?.live, ...tenants.map(([, r]) => r?.live)].filter(Boolean));

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Vector tables</h1>
          <p className="muted small">Read-only. Switching or retiring tables stays in the CLI.</p>
        </div>
        <button type="button" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      <ErrorBanner error={error} />
      {!data && !error && <Loading />}

      {data && (
        <>
          {data.consistent === false && (
            <div className="alert alert-error" role="alert">
              Inconsistent: <code>chunks_active</code> serves <code>{data.active_table ?? "-"}</code>, but the default
              registry's live table is <code>{registry?.live ?? "-"}</code>.
            </div>
          )}

          <div className="card">
            <h2>Default registry</h2>
            <dl className="stats">
              <Stat label="Live table" value={registry?.live ? <code>{registry.live}</code> : "-"} />
              <Stat label="Writes" value={writes(registry)} />
              <Stat label="Epoch" value={num(registry?.epoch)} />
              <Stat label="chunks_active serves" value={data.active_table ? <code>{data.active_table}</code> : "-"} />
              <Stat
                label="Consistent"
                value={
                  data.consistent === false ? (
                    <span className="badge badge-bad">no</span>
                  ) : data.consistent ? (
                    <span className="badge badge-ok">yes</span>
                  ) : (
                    "-"
                  )
                }
              />
            </dl>
          </div>

          <h2>Tenant registries</h2>
          {tenants.length === 0 ? (
            <p className="muted">No tenant has its own registry; every tenant uses the default one.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Tenant</th>
                    <th scope="col">Live table</th>
                    <th scope="col">Writes</th>
                    <th scope="col" className="num">
                      Epoch
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {tenants.map(([tenant, reg]) => (
                    <tr key={tenant}>
                      <td>
                        <code>{tenant}</code>
                      </td>
                      <td>{reg?.live ? <code>{reg.live}</code> : "-"}</td>
                      <td>{writes(reg)}</td>
                      <td className="num">{num(reg?.epoch)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2>Tables</h2>
          {tables.length === 0 ? (
            <p className="muted">No tables reported.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Table</th>
                    <th scope="col" className="num">
                      Rows
                    </th>
                    <th scope="col" className="num">
                      Active rows
                    </th>
                    <th scope="col" className="num">
                      Active articles
                    </th>
                    <th scope="col">Live for</th>
                  </tr>
                </thead>
                <tbody>
                  {tables.map(([name, raw]) => {
                    const stats: TableStats | null = isRecord(raw) ? raw : null;
                    const liveFor = [
                      ...(registry?.live === name ? ["default"] : []),
                      ...tenants.filter(([, r]) => r?.live === name).map(([t]) => t),
                    ];
                    return (
                      <tr key={name} className={liveTables.has(name) ? "selected" : undefined}>
                        <td>
                          <code>{name}</code>
                        </td>
                        {stats ? (
                          <>
                            <td className="num">{num(stats.rows)}</td>
                            <td className="num">{num(stats.active_rows)}</td>
                            <td className="num">{num(stats.active_articles)}</td>
                          </>
                        ) : (
                          <td colSpan={3} className="muted">
                            Not created yet
                          </td>
                        )}
                        <td>{liveFor.length > 0 ? liveFor.join(", ") : "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {profiles.length > 0 && (
            <>
              <h2>Embedding profiles</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Profile</th>
                      <th scope="col">Table</th>
                      <th scope="col">Provider</th>
                      <th scope="col">Model</th>
                      <th scope="col" className="num">
                        Dimension
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {profiles.map(([name, p]) => (
                      <tr key={name}>
                        <td>{name}</td>
                        <td>{p?.table ? <code>{p.table}</code> : "-"}</td>
                        <td>{p?.provider ?? "-"}</td>
                        <td>{p?.model ?? "-"}</td>
                        <td className="num">{num(p?.dimension)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}
