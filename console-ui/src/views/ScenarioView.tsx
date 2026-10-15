import { useState, type ReactNode } from "react";
import type { DataSource } from "../api";
import { Collapsible, DiffText, EmptyState, ErrorState, Loading, PageHead, Stat, Status, ToolChips } from "../components/common";
import { explain, humanize, verdictOf, VERDICT_LABEL, VERDICT_TONE } from "../explain";
import { formatMs, formatTokens, inlineJson } from "../format";
import { useAsync } from "../hooks";
import { href } from "../router";
import { readableDiff } from "../textdiff";
import type { DriftResult, Finding, Trace } from "../types";
import { DiffRows } from "./CompareView";

type Loaded = { kind: "pair"; drift: DriftResult } | { kind: "single"; trace: Trace } | { kind: "missing" };

async function load(source: DataSource, name: string): Promise<Loaded> {
  const runs = (await source.runs()).filter((r) => r.name === name);
  const hasBaseline = runs.some((r) => r.kind === "baseline");
  const hasCandidate = runs.some((r) => r.kind === "candidate");
  if (hasBaseline && hasCandidate) return { kind: "pair", drift: await source.drift(name) };
  if (hasBaseline) return { kind: "single", trace: await source.run(`baseline:${name}`) };
  return { kind: "missing" };
}

type Pending = "accept" | "discard" | null;

/** Accept or discard the latest run, with one inline confirmation step. */
function Decision({ source, name, onDone }: { source: DataSource; name: string; onDone: (message: string) => void }) {
  const [confirming, setConfirming] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!source.writable) {
    return (
      <p className="decision-note">
        {source.workspace === "sample" ? (
          "The sample workspace is read-only. In your own project you accept or discard changes right here."
        ) : (
          <>
            To accept this change, run <code>replex approve {name}</code> in your project.
          </>
        )}
      </p>
    );
  }

  const act = async (what: "accept" | "discard") => {
    setBusy(true);
    setError(null);
    try {
      if (what === "accept") await source.accept(name);
      else await source.discard(name);
      onDone(
        what === "accept"
          ? "Accepted. The latest run is now the recorded behaviour; commit the updated cassette."
          : "Discarded. The recorded behaviour is unchanged.",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  if (confirming) {
    return (
      <div className="decision" role="group" aria-label="Confirm">
        <p>
          {confirming === "accept"
            ? "Replace the recorded behaviour with the latest run?"
            : "Delete the latest run and keep the recorded behaviour?"}
        </p>
        <button type="button" className="button" disabled={busy} onClick={() => act(confirming)}>
          {confirming === "accept" ? "Accept" : "Discard"}
        </button>
        <button type="button" className="link" disabled={busy} onClick={() => setConfirming(null)}>
          Cancel
        </button>
      </div>
    );
  }
  return (
    <div className="decision">
      <button type="button" className="button" onClick={() => setConfirming("accept")}>
        Accept new behaviour
      </button>
      <button type="button" className="link" onClick={() => setConfirming("discard")}>
        Discard latest run
      </button>
      {error && <p className="text-fail">{error}</p>}
    </div>
  );
}

function FindingRow({ f }: { f: Finding }) {
  return (
    <li className={`finding finding-${f.severity}`}>
      <div className="finding-check">
        {f.check} · {f.severity === "fail" ? "fails the policy" : f.severity === "warn" ? "warning" : "reported only"}
      </div>
      <p className="finding-message">{f.message}</p>
      {(f.before !== null || f.after !== null) && f.check !== "latency" && (
        <div className="finding-values">
          <span className="change-before">{inlineJson(f.before, 140)}</span>
          <span className="change-arrow" aria-hidden="true">
            →
          </span>
          <span className="change-after">{inlineJson(f.after, 140)}</span>
        </div>
      )}
    </li>
  );
}

function Versus({ drift }: { drift: DriftResult }) {
  const b = drift.report.baseline;
  const c = drift.report.candidate;
  const known = new Set(b.tool_names);
  const fresh = new Set(c.tool_names.filter((n) => !known.has(n)));
  const rows: { label: string; before: ReactNode; after: ReactNode }[] = [
    { label: "Tools, in order", before: <ToolChips names={b.tool_names} />, after: <ToolChips names={c.tool_names} highlight={fresh} /> },
    { label: "Steps", before: b.steps, after: c.steps },
    { label: "Tokens", before: formatTokens(b.input_tokens + b.output_tokens), after: formatTokens(c.input_tokens + c.output_tokens) },
    { label: "Latency", before: formatMs(b.latency_ms), after: formatMs(c.latency_ms) },
    { label: "Model", before: b.model, after: c.model },
  ];
  return (
    <div className="versus">
      <div className="versus-head">
        <div />
        <div>
          Recorded
          <a className="versus-link" href={href({ view: "run", id: `baseline:${drift.name}`, step: null })}>
            Open run
          </a>
        </div>
        <div>
          Latest
          <a className="versus-link" href={href({ view: "run", id: `candidate:${drift.name}`, step: null })}>
            Open run
          </a>
        </div>
      </div>
      {rows.map((r) => (
        <div key={r.label} className="versus-row">
          <div className="versus-label">{r.label}</div>
          <div>{r.before}</div>
          <div>{r.after}</div>
        </div>
      ))}
      <div className="versus-row versus-wide">
        <div className="versus-label">Final answer</div>
        <div>
          {b.final_output === c.final_output ? (
            <p className="final">{c.final_output || <span className="quiet">(empty)</span>}</p>
          ) : (
            <DiffText pieces={readableDiff(b.final_output, c.final_output)} />
          )}
        </div>
      </div>
    </div>
  );
}

export function ScenarioView({ source, name }: { source: DataSource; name: string }) {
  const [revision, setRevision] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const data = useAsync(() => load(source, name), [source, name, revision]);

  const overview = (
    <a href={href({ view: "overview" })} className="crumb">
      Overview
    </a>
  );
  if (data.state === "loading") return <Loading what="scenario" />;
  if (data.state === "error") return <ErrorState error={data.error} />;
  const loaded = data.data;

  if (loaded.kind === "missing") {
    return (
      <section>
        <PageHead eyebrow={overview} title={humanize(name)} />
        <EmptyState title="There is no recording with this name.">
          {notice ?? "It may have been renamed or deleted."}
        </EmptyState>
      </section>
    );
  }

  if (loaded.kind === "single") {
    const t = loaded.trace;
    return (
      <section>
        <PageHead
          eyebrow={
            <>
              {overview}
              <span className="crumb-sep" aria-hidden="true">
                ›
              </span>
              <Status tone="neutral">{VERDICT_LABEL.pending}</Status>
            </>
          }
          title={humanize(name)}
          lede="This is the recorded behaviour. After your next change, run a drift check to see whether the agent still does the same thing."
          actions={
            <a className="button" href={href({ view: "run", id: `baseline:${name}`, step: null })}>
              Open the recorded run
            </a>
          }
        />
        {notice && (
          <p className="notice" role="status">
            {notice}
          </p>
        )}
        <div className="stats">
          <Stat label="Steps" value={t.steps.length} />
          <Stat label="Tool calls" value={t.steps.filter((s) => s.kind === "tool").length} />
          <Stat label="Tokens" value={formatTokens(t.usage.input_tokens + t.usage.output_tokens)} />
        </div>
        <div className="panel">
          <div className="panel-row">
            <div className="panel-label">Tools, in order</div>
            <ToolChips names={t.steps.flatMap((s) => (s.kind === "tool" ? [s.name] : []))} />
          </div>
          <div className="panel-row">
            <div className="panel-label">Final answer</div>
            <p className="final">{t.final_output || <span className="quiet">(empty)</span>}</p>
          </div>
          <div className="panel-row">
            <div className="panel-label">Check it</div>
            <code>pytest --replex-mode=drift</code>
          </div>
        </div>
      </section>
    );
  }

  const { drift } = loaded;
  const report = drift.report;
  const verdict = verdictOf(report);
  const changes = explain(report);
  const done = (message: string) => {
    setNotice(message);
    setRevision((n) => n + 1);
  };

  return (
    <section>
      <PageHead
        eyebrow={
          <>
            {overview}
            <span className="crumb-sep" aria-hidden="true">
              ›
            </span>
            <Status tone={VERDICT_TONE[verdict]}>{VERDICT_LABEL[verdict]}</Status>
          </>
        }
        title={humanize(name)}
        lede={
          verdict === "changed"
            ? "The latest run breaks your policy. Accept it if the new behaviour is what you wanted; otherwise fix the change and run the check again."
            : verdict === "minor"
              ? "The latest run differs in ways your policy allows. Review the differences, then accept or discard them."
              : "The latest run does the same thing as the recorded one."
        }
        actions={<Decision source={source} name={name} onDone={done} />}
      />
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}

      <div className="section-head section-head-first">
        <h2>What changed</h2>
      </div>
      {changes.length ? (
        <ul className="changes-list">
          {changes.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      ) : (
        <p className="secondary">Nothing your policy checks for.</p>
      )}

      <div className="section-head">
        <h2>Recorded and latest, side by side</h2>
      </div>
      <Versus drift={drift} />

      <div className="section-head">
        <h2>Step by step</h2>
        <a className="cta" href={href({ view: "compare", a: `baseline:${name}`, b: `candidate:${name}`, step: null })}>
          Open in Compare
        </a>
      </div>
      <DiffRows result={{ a: drift.baseline, b: drift.candidate, diff: report.diff, report }} focusStep={null} />

      <div className="section-head">
        <h2>Policy details</h2>
      </div>
      <Collapsible title={`${report.findings.length} finding${report.findings.length === 1 ? "" : "s"} from the policy checks`}>
        <ul className="findings">
          {report.findings.map((f, i) => (
            <FindingRow key={i} f={f} />
          ))}
        </ul>
      </Collapsible>
    </section>
  );
}
