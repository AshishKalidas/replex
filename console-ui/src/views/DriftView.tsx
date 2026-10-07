import type { DataSource } from "../api";
import { DiffText, DriftPill, EmptyState, ErrorState, Loading, Pill, ToolChips } from "../components/common";
import { formatMs, formatTokens, inlineJson } from "../format";
import { useAsync, useKeyNav } from "../hooks";
import { href } from "../router";
import { readableDiff } from "../textdiff";
import type { Finding, Severity } from "../types";

const GROUPS: { severity: Severity; title: string; tone: string }[] = [
  { severity: "fail", title: "Failures", tone: "fail" },
  { severity: "warn", title: "Warnings", tone: "warn" },
  { severity: "info", title: "Reported", tone: "info" },
];

function FindingRow({ f, name, navIndex, active }: { f: Finding; name: string; navIndex: number; active: boolean }) {
  const link =
    f.step_index === null
      ? null
      : href({ view: "compare", a: `baseline:${name}`, b: `candidate:${name}`, step: f.step_index });
  return (
    <li className={`finding finding-${f.severity}${active ? " row-active" : ""}`} data-nav-index={navIndex}>
      <div className="finding-head">
        <code className="finding-check">{f.check}</code>
        <span className="finding-message">{f.message}</span>
        {link && (
          <a className="finding-link" href={link}>
            step {f.step_index} →
          </a>
        )}
      </div>
      {(f.before !== null || f.after !== null) && f.check !== "latency" && (
        <div className="finding-values">
          <span className="change-before">{inlineJson(f.before, 140)}</span>
          <span className="change-arrow">→</span>
          <span className="change-after">{inlineJson(f.after, 140)}</span>
        </div>
      )}
    </li>
  );
}

export function DriftView({ source, name }: { source: DataSource; name: string }) {
  const drift = useAsync(() => source.drift(name), [source, name]);
  const findings = drift.state === "ready" ? GROUPS.flatMap((g) => drift.data.report.findings.filter((f) => f.severity === g.severity)) : [];
  const [active] = useKeyNav(findings.length, (i) => {
    const f = findings[i];
    if (f && f.step_index !== null) {
      window.location.hash = href({ view: "compare", a: `baseline:${name}`, b: `candidate:${name}`, step: f.step_index });
    }
  });

  if (drift.state === "loading") return <Loading what="drift report" />;
  if (drift.state === "error") return <ErrorState error={drift.error} />;
  const { report } = drift.data;
  const b = report.baseline;
  const c = report.candidate;
  const known = new Set(b.tool_names);
  const fresh = new Set(c.tool_names.filter((n) => !known.has(n)));
  const fails = report.findings.filter((f) => f.severity === "fail").length;
  const warns = report.findings.filter((f) => f.severity === "warn").length;
  let nav = 0;

  return (
    <section>
      <header className="page-head">
        <div>
          <div className="crumbs">
            <a href={href({ view: "drift-index" })}>Drift</a> / {name}
          </div>
          <h1>{name}</h1>
        </div>
        <div className="toolbar">
          <a className="button" href={href({ view: "compare", a: `baseline:${name}`, b: `candidate:${name}`, step: null })}>
            Side-by-side compare
          </a>
        </div>
      </header>

      <div className={`banner ${report.passed ? "banner-ok" : "banner-fail"}`}>
        <strong>{report.passed ? "Behaviour within policy" : "Behaviour drifted"}</strong>
        <span>
          {fails} failure{fails === 1 ? "" : "s"} · {warns} warning{warns === 1 ? "" : "s"}
        </span>
      </div>

      <table className="summary">
        <thead>
          <tr>
            <th />
            <th>Baseline</th>
            <th>Candidate</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th>Model</th>
            <td>
              {b.provider} · <code>{b.model}</code>
            </td>
            <td>
              {c.provider} · <code>{c.model}</code>
            </td>
          </tr>
          <tr>
            <th>Tools</th>
            <td>
              <ToolChips names={b.tool_names} />
            </td>
            <td>
              <ToolChips names={c.tool_names} highlight={fresh} />
            </td>
          </tr>
          <tr>
            <th>Steps</th>
            <td>{b.steps}</td>
            <td>{c.steps}</td>
          </tr>
          <tr>
            <th>Tokens</th>
            <td>{formatTokens(b.input_tokens + b.output_tokens)}</td>
            <td>{formatTokens(c.input_tokens + c.output_tokens)}</td>
          </tr>
          <tr>
            <th>Latency</th>
            <td>{formatMs(b.latency_ms)}</td>
            <td>{formatMs(c.latency_ms)}</td>
          </tr>
          <tr>
            <th>Final output</th>
            <td colSpan={2}>
              {b.final_output === c.final_output ? (
                <p className="final">{c.final_output || <span className="muted">(empty)</span>}</p>
              ) : (
                <DiffText pieces={readableDiff(b.final_output, c.final_output)} />
              )}
            </td>
          </tr>
        </tbody>
      </table>

      {GROUPS.map((g) => {
        const group = report.findings.filter((f) => f.severity === g.severity);
        if (!group.length) return null;
        return (
          <div key={g.severity} className="findings-group">
            <h2>
              {g.title} <Pill tone={g.tone}>{group.length}</Pill>
            </h2>
            <ul className="findings">
              {group.map((f, k) => {
                const i = nav++;
                return <FindingRow key={`${g.severity}-${k}`} f={f} name={name} navIndex={i} active={i === active} />;
              })}
            </ul>
          </div>
        );
      })}
      {report.findings.length === 0 && <EmptyState title="No findings">Every check in the policy passed.</EmptyState>}
    </section>
  );
}

export function DriftIndex({ source }: { source: DataSource }) {
  const runs = useAsync(() => source.runs(), [source]);
  const rows = runs.state === "ready" ? runs.data.filter((r) => r.kind === "candidate" && r.drift) : [];
  const [active] = useKeyNav(rows.length, (i) => {
    const r = rows[i];
    if (r) window.location.hash = href({ view: "drift", name: r.name });
  });
  if (runs.state === "loading") return <Loading what="drift results" />;
  if (runs.state === "error") return <ErrorState error={runs.error} />;
  return (
    <section>
      <header className="page-head">
        <div>
          <h1>Drift</h1>
          <p className="muted">Candidates from drift runs, compared with their baselines under the policy.</p>
        </div>
      </header>
      {rows.length === 0 ? (
        <EmptyState title="No drift runs yet">
          Run <code>pytest --replex-mode=drift</code> to record candidates next to your baselines.
        </EmptyState>
      ) : (
        <ul className="drift-list">
          {rows.map((r, i) => (
            <li key={r.id} data-nav-index={i} className={i === active ? "row-active" : ""}>
              <a href={href({ view: "drift", name: r.name })}>
                <span className="drift-name">{r.name}</span>
                <ToolChips names={r.tool_names ?? []} />
                <DriftPill drift={r.drift} warnings={r.warnings} />
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
