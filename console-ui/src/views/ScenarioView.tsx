import { useState, type ReactNode } from "react";
import type { DataSource } from "../api";
import { Collapsible, DiffText, EmptyState, ErrorState, Loading, PageHead, Status, ToolChips } from "../components/common";
import { explain, humanize, verdictOf, VERDICT_LABEL, VERDICT_TONE } from "../explain";
import { actions, compareActions, customerMessage, reply, type Action } from "../story";
import { formatMs, formatTokens, inlineJson } from "../format";
import { useAsync } from "../hooks";
import { href } from "../router";
import { readableDiff } from "../textdiff";
import type { DriftResult, Finding, Trace } from "../types";

function lowerFirst(text: string): string {
  return text ? text[0]!.toLowerCase() + text.slice(1) : text;
}
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

/** The question a review ends with, and the two answers. One inline confirmation each. */
function Decision({ source, name, onDone }: { source: DataSource; name: string; onDone: (message: string) => void }) {
  const [confirming, setConfirming] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (what: "accept" | "discard") => {
    setBusy(true);
    setError(null);
    try {
      if (what === "accept") await source.accept(name);
      else await source.discard(name);
      onDone(
        what === "accept"
          ? "Accepted. This is now the expected behaviour. Commit the updated recording so your tests use it."
          : "Rejected. The latest run is gone and the expected behaviour is unchanged. Fix the change, then check again.",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  let controls: ReactNode;
  if (!source.writable) {
    controls = (
      <p className="decision-note">
        {source.workspace === "sample" ? (
          "This sample is read-only. In your own project you answer right here: accept the new behaviour, or reject it and fix the change."
        ) : (
          <>
            Answer from your project with <code>replex approve {name}</code>, or by running the console there.
          </>
        )}
      </p>
    );
  } else if (confirming) {
    controls = (
      <div className="decision-buttons" role="group" aria-label="Confirm">
        <p>
          {confirming === "accept"
            ? "Save the latest run as the new expected behaviour?"
            : "Throw away the latest run and keep the expected behaviour?"}
        </p>
        <button type="button" className="button" disabled={busy} onClick={() => act(confirming)}>
          {confirming === "accept" ? "Accept" : "Reject"}
        </button>
        <button type="button" className="link" disabled={busy} onClick={() => setConfirming(null)}>
          Cancel
        </button>
      </div>
    );
  } else {
    controls = (
      <>
        <div className="decision-buttons">
          <button type="button" className="button" onClick={() => setConfirming("accept")}>
            Yes, accept the new behaviour
          </button>
          <button type="button" className="button button-secondary" onClick={() => setConfirming("discard")}>
            No, reject it
          </button>
        </div>
        <p className="decision-note">
          Accepting saves the latest run as the new recording, so your tests expect it from now on. Rejecting
          throws the latest run away; fix your change and run the check again.
        </p>
      </>
    );
  }

  return (
    <div className="decision">
      <h2>Is this what you wanted?</h2>
      {controls}
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

function ActionList({ list }: { list: Action[] }) {
  if (!list.length) return <p className="quiet">Used no tools.</p>;
  return (
    <ol className="actions-list">
      {list.map((a, i) => (
        <li key={i} className={`action action-${a.status}`}>
          <span className="action-label">
            <span className="action-name">{a.label}</span>
            {a.status === "removed" && <span className="action-tag">Skipped</span>}
            {a.status === "added" && <span className="action-tag">New</span>}
            {a.error && <span className="action-tag action-tag-error">Failed</span>}
          </span>
          {a.detail && <span className="action-detail">{a.detail}</span>}
        </li>
      ))}
    </ol>
  );
}

function ReplyBlock({ output }: { output: string }) {
  const r = reply(output);
  return (
    <div className="reply">
      {r.text ? <p className="reply-text">{r.text}</p> : <p className="quiet">No reply.</p>}
      {r.facts.length > 0 && (
        <dl className="reply-facts">
          {r.facts.map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

function CustomerSaid({ trace }: { trace: Trace }) {
  const said = customerMessage(trace);
  if (!said) return null;
  return (
    <figure className="customer">
      <figcaption>The customer wrote</figcaption>
      <blockquote>{said}</blockquote>
    </figure>
  );
}

/** The recorded and latest runs told as what the agent did and said. */
function Conversation({ drift }: { drift: DriftResult }) {
  const { before, after } = compareActions(actions(drift.baseline), actions(drift.candidate));
  const sameReply = drift.baseline.final_output === drift.candidate.final_output;
  return (
    <section className="conversation" aria-label="The conversation, before and after">
      <CustomerSaid trace={drift.baseline} />
      <div className="before-after">
        <div className="side">
          <h3>Before your change</h3>
          <div className="side-label">What the agent did</div>
          <ActionList list={before} />
          <div className="side-label">What it answered</div>
          <ReplyBlock output={drift.baseline.final_output} />
        </div>
        <div className="side">
          <h3>After your change</h3>
          <div className="side-label">What the agent did</div>
          <ActionList list={after} />
          <div className="side-label">What it answered{sameReply ? " (unchanged)" : ""}</div>
          <ReplyBlock output={drift.candidate.final_output} />
        </div>
      </div>
    </section>
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
          lede="This is how the agent handled this conversation when it was recorded. RepleX expects it to keep doing the same."
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
        <section className="conversation" aria-label="The recorded conversation">
          <CustomerSaid trace={t} />
          <div className="before-after before-after-single">
            <div className="side">
              <h3>What the agent does</h3>
              <ActionList list={actions(t)} />
              <div className="side-label">What it answers</div>
              <ReplyBlock output={t.final_output} />
            </div>
          </div>
        </section>
        <p className="decision-note pending-note">
          After your next change to the prompt, model or code, run <code>pytest --replex-mode=drift</code> and this
          page will show whether the agent still does the same thing.
        </p>
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
          verdict === "same"
            ? "The agent still handles this conversation the same way it did when it was recorded."
            : `After your change, the agent ${lowerFirst(changes[0] ?? "behaves differently.")}`
        }
      />
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}

      <Conversation drift={drift} />

      {verdict === "same" ? (
        <p className="decision-note pending-note">Nothing to decide: this scenario behaves as recorded.</p>
      ) : (
        <Decision source={source} name={name} onDone={done} />
      )}

      {changes.length > 1 && (
        <>
          <div className="section-head">
            <h2>Everything that changed</h2>
          </div>
          <ul className="changes-list">
            {changes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </>
      )}

      <div className="details-divider">
        <h2>The details</h2>
        <p>For engineers: the numbers, every model and tool call, and the raw policy results.</p>
      </div>

      <div className="section-head">
        <h2>In numbers</h2>
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
