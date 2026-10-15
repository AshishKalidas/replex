import { useEffect, useMemo, useRef, useState } from "react";
import type { DataSource, WorkspaceInfo } from "../api";
import { DriftStatus, EmptyState, ErrorState, KindLabel, Loading, PageHead, Segmented, Stat } from "../components/common";
import { humanize } from "../explain";
import { filterRuns, formatDate, formatMs, formatTokens, type RunFilter } from "../format";
import { useAsync, useKeyNav } from "../hooks";
import { href } from "../router";
import type { RunSummary } from "../types";

const FILTERS: { id: RunFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "baseline", label: "Recorded" },
  { id: "candidate", label: "Latest runs" },
  { id: "failed", label: "Changed" },
];

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** One sentence that says what is in the workspace, the way a person would. */
function summary(runs: RunSummary[]): string {
  const scenarios = new Set(runs.map((r) => r.name)).size;
  const candidates = runs.filter((r) => r.kind === "candidate");
  const drifted = candidates.filter((r) => r.drift === "failed").length;
  const recorded = `${plural(scenarios, "scenario")} recorded.`;
  if (!candidates.length) return `${recorded} Nothing has been re-run yet.`;
  if (!drifted) return `${recorded} Every latest run behaves as recorded.`;
  return `${recorded} ${drifted} of ${candidates.length} behave differently after the last change.`;
}

function ProjectOnboarding({
  workspace,
  alternatives,
  onSwitchWorkspace,
}: {
  workspace: WorkspaceInfo;
  alternatives: WorkspaceInfo[];
  onSwitchWorkspace?: (id: string) => void;
}) {
  const sample = alternatives.find((w) => w.id === "sample");
  return (
    <EmptyState
      title="No recordings yet."
      action={
        sample &&
        onSwitchWorkspace && (
          <button type="button" className="button" onClick={() => onSwitchWorkspace(sample.id)}>
            Explore the sample workspace
          </button>
        )
      }
    >
      <p>
        RepleX is watching <code>{workspace.baseline_dir}</code>. Record a run and it shows up here
        the next time you look.
      </p>
      <ol className="steps">
        <li>
          Mark a test with <code>@pytest.mark.replex</code> and decorate your tools with <code>@replex.tool</code>.
        </li>
        <li>
          Record once with a real key: <code>pytest --replex-mode=record</code>
        </li>
        <li>
          After a prompt or model change: <code>pytest --replex-mode=drift</code>
        </li>
      </ol>
    </EmptyState>
  );
}

export function RunsView({
  source,
  initialQuery,
  workspace,
  alternatives = [],
  onSwitchWorkspace,
}: {
  source: DataSource;
  initialQuery: string;
  workspace?: WorkspaceInfo;
  alternatives?: WorkspaceInfo[];
  onSwitchWorkspace?: (id: string) => void;
}) {
  const runs = useAsync(() => source.runs(), [source]);
  const [query, setQuery] = useState(initialQuery);
  const [filter, setFilter] = useState<RunFilter>("all");
  const search = useRef<HTMLInputElement>(null);

  const rows = useMemo(
    () =>
      runs.state === "ready"
        ? // Each scenario's recorded run directly above its latest run.
          filterRuns(runs.data, query, filter).sort(
            (a, b) => a.name.localeCompare(b.name) || (a.kind === b.kind ? 0 : a.kind === "baseline" ? -1 : 1),
          )
        : [],
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

  const eyebrow = workspace?.label ?? source.label;
  if (runs.state === "loading") return <Loading what="runs" />;
  if (runs.state === "error") return <ErrorState error={runs.error} />;
  if (runs.data.length === 0) {
    return (
      <section>
        <PageHead eyebrow={eyebrow} title="Runs" />
        {workspace ? (
          <ProjectOnboarding workspace={workspace} alternatives={alternatives} onSwitchWorkspace={onSwitchWorkspace} />
        ) : (
          <EmptyState title="This report is empty." />
        )}
      </section>
    );
  }

  const all = runs.data;
  const candidates = all.filter((r) => r.kind === "candidate");
  const drifted = candidates.filter((r) => r.drift === "failed").length;
  const hasDates = all.some((r) => r.created_at);
  const withLatest = new Set(candidates.map((r) => r.name));

  return (
    <section>
      <PageHead eyebrow={eyebrow} title="Runs" lede={summary(all)} />

      <div className="stats">
        <Stat label="Recorded runs" value={all.length - candidates.length} />
        <Stat label="Latest runs" value={candidates.length} />
        <Stat label="Behaviour changed" value={drifted} tone={drifted ? "fail" : undefined} />
      </div>

      <div className="toolbar">
        <input
          ref={search}
          className="search"
          type="search"
          placeholder="Search runs, models and tools"
          aria-label="Search runs"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <Segmented label="Filter runs" value={filter} options={FILTERS} onChange={(id) => setFilter(id as RunFilter)} />
      </div>

      {rows.length === 0 ? (
        <EmptyState title="Nothing matches.">Try another search or filter.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="runs">
            <thead>
              <tr>
                <th>Run</th>
                <th>Model</th>
                <th className="num">Steps</th>
                <th className="num">Tokens</th>
                <th className="num">Latency</th>
                {hasDates && <th>Date</th>}
                <th>Status</th>
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
                      {humanize(r.name)}
                    </a>
                    <div className="run-sub">
                      <KindLabel kind={r.kind} />
                      <code>{r.name}</code>
                      {r.tool_calls !== undefined && <span>{plural(r.tool_calls, "tool call")}</span>}
                    </div>
                    {r.error && <div className="text-fail">{r.error}</div>}
                  </td>
                  <td>
                    <div>{r.model}</div>
                    <div className="run-sub">{r.provider}</div>
                  </td>
                  <td className="num">{r.steps ?? "—"}</td>
                  <td className="num">{formatTokens(r.tokens)}</td>
                  <td className="num">{formatMs(r.latency_ms)}</td>
                  {hasDates && <td className="secondary">{formatDate(r.created_at)}</td>}
                  <td>
                    {r.kind === "candidate" ? (
                      <a className="status-link" href={href({ view: "scenario", name: r.name })}>
                        <DriftStatus drift={r.drift} warnings={r.warnings} />
                      </a>
                    ) : withLatest.has(r.name) ? (
                      <span className="quiet">—</span>
                    ) : (
                      <DriftStatus drift={null} />
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
