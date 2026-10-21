import type { DataSource } from "../api";
import { consistencyHeadline, spreadText, variantPath, variantShare } from "../consistency";
import { formatMs, formatTokens } from "../format";
import { useAsync } from "../hooks";
import type { ConsistencyReport } from "../types";
import "../consistency.css";

/** The paths a scenario's repeated runs took, most common first. */
export function ConsistencyPanel({ report }: { report: ConsistencyReport }) {
  return (
    <section className="consistency" aria-label="Consistency across repeated runs">
      <div className="section-head">
        <h2>How consistent is it?</h2>
      </div>
      <p className="consistency-lede">{consistencyHeadline(report)}</p>
      <ol className="consistency-paths">
        {report.variants.map((v) => (
          <li key={`${v.tools.join(">")}|${v.category ?? ""}`} className="consistency-path">
            <div className="consistency-share">
              <span className="consistency-count">{variantShare(v, report.runs)}</span>
              {v.matches_baseline && <span className="consistency-tag">Recorded path</span>}
            </div>
            <div className="consistency-bar" aria-hidden="true">
              <span style={{ width: `${Math.round(v.share * 100)}%` }} />
            </div>
            <p className="consistency-steps">{variantPath(v)}</p>
          </li>
        ))}
      </ol>
      <dl className="consistency-numbers">
        <div>
          <dt>Tokens</dt>
          <dd>{spreadText(report.tokens, formatTokens)}</dd>
        </div>
        <div>
          <dt>Time</dt>
          <dd>{spreadText(report.latency_ms, formatMs)}</dd>
        </div>
        <div>
          <dt>Steps</dt>
          <dd>{spreadText(report.steps, String)}</dd>
        </div>
      </dl>
    </section>
  );
}

/** Loads the consistency report of a scenario; renders nothing when it was never run repeatedly. */
export function Consistency({ source, name }: { source: DataSource; name: string }) {
  const data = useAsync(() => source.consistency(name), [source, name]);
  if (data.state !== "ready" || data.data === null) return null;
  return <ConsistencyPanel report={data.data} />;
}
