import { useEffect, useMemo, useRef, useState } from "react";
import type { DataSource } from "../api";
import { DriftPill, EmptyState, ErrorState, KindBadge, Loading } from "../components/common";
import { filterRuns, formatDate, formatMs, formatTokens, type RunFilter } from "../format";
import { useAsync, useKeyNav } from "../hooks";
import { href } from "../router";

const FILTERS: { id: RunFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "baseline", label: "Baselines" },
  { id: "candidate", label: "Candidates" },
  { id: "failed", label: "Drift failed" },
];

export function RunsView({ source, initialQuery }: { source: DataSource; initialQuery: string }) {
  const runs = useAsync(() => source.runs(), [source]);
  const [query, setQuery] = useState(initialQuery);
  const [filter, setFilter] = useState<RunFilter>("all");
  const search = useRef<HTMLInputElement>(null);

  const rows = useMemo(
    () => (runs.state === "ready" ? filterRuns(runs.data, query, filter) : []),
    [runs, query, filter],
  );
  const [active, setActive] = useKeyNav(rows.length, (i) => {
    const row = rows[i];
    if (row) window.location.hash = href({ view: "run", id: row.id, step: null });
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "/" && document.activeElement !== search.current) {
        e.preventDefault();
        search.current?.focus();
      }
      if (e.key === "Escape" && document.activeElement === search.current) search.current?.blur();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (runs.state === "loading") return <Loading what="runs" />;
  if (runs.state === "error") return <ErrorState error={runs.error} />;
  if (runs.data.length === 0) {
    return (
      <EmptyState title="No cassettes yet">
        Record one with <code>pytest --replex-mode=record</code>, then refresh.
      </EmptyState>
    );
  }
  const failed = runs.data.filter((r) => r.drift === "failed" && r.kind === "candidate").length;

  return (
    <section>
      <header className="page-head">
        <div>
          <h1>Runs</h1>
          <p className="muted">
            {runs.data.filter((r) => r.kind === "baseline").length} baselines ·{" "}
            {runs.data.filter((r) => r.kind === "candidate").length} candidates
            {failed > 0 && (
              <>
                {" "}· <span className="text-fail">{failed} drifted</span>
              </>
            )}
          </p>
        </div>
        <div className="toolbar">
          <input
            ref={search}
            className="search"
            type="search"
            placeholder="Search name, model, tool…  ( / )"
            aria-label="Search runs"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="segmented" role="tablist" aria-label="Filter runs">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                role="tab"
                aria-selected={filter === f.id}
                className={filter === f.id ? "on" : ""}
                onClick={() => setFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      </header>
      {rows.length === 0 ? (
        <EmptyState title="Nothing matches">Try another search or filter.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="runs">
            <thead>
              <tr>
                <th>Run</th>
                <th>Model</th>
                <th className="num">Steps</th>
                <th className="num">Tools</th>
                <th className="num">Tokens</th>
                <th className="num">Latency</th>
                <th>Recorded</th>
                <th>Drift</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={r.id}
                  data-nav-index={i}
                  className={i === active ? "row-active" : ""}
                  onMouseEnter={() => setActive(i)}
                >
                  <td>
                    <a className="run-link" href={href({ view: "run", id: r.id, step: null })}>
                      {r.name}
                    </a>{" "}
                    <KindBadge kind={r.kind} />
                    {r.error && <div className="text-fail small">{r.error}</div>}
                  </td>
                  <td>
                    <span className="muted">{r.provider}</span> {r.model}
                  </td>
                  <td className="num">{r.steps ?? "—"}</td>
                  <td className="num">{r.tool_calls ?? "—"}</td>
                  <td className="num">{formatTokens(r.tokens)}</td>
                  <td className="num">{formatMs(r.latency_ms)}</td>
                  <td className="muted">{formatDate(r.created_at)}</td>
                  <td>
                    {r.drift ? (
                      <a href={href({ view: "drift", name: r.name })}>
                        <DriftPill drift={r.drift} warnings={r.warnings} />
                      </a>
                    ) : (
                      <DriftPill drift={null} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
