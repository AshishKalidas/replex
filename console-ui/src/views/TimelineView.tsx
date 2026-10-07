import { useState } from "react";
import { splitRunId, type DataSource } from "../api";
import { ErrorState, KindBadge, Loading, Stat, ToolChips } from "../components/common";
import { StepCard } from "../components/StepCard";
import { formatMs, formatTokens } from "../format";
import { useAsync, useKeyNav } from "../hooks";
import { href } from "../router";

export function TimelineView({ source, id, step }: { source: DataSource; id: string; step: number | null }) {
  const trace = useAsync(() => source.run(id), [source, id]);
  const steps = trace.state === "ready" ? trace.data.steps : [];
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set(step === null ? [] : [step]));
  const toggle = (i: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  const [active, setActive] = useKeyNav(steps.length, toggle, step);
  const { kind, name } = splitRunId(id);

  if (trace.state === "loading") return <Loading what="run" />;
  if (trace.state === "error") return <ErrorState error={trace.error} />;
  const t = trace.data;
  const compareTarget =
    kind === "candidate"
      ? { a: `baseline:${name}`, b: id }
      : { a: id, b: `candidate:${name}` };
  const canCompare = source.comparable(compareTarget.a, compareTarget.b);

  return (
    <section>
      <header className="page-head">
        <div>
          <div className="crumbs">
            <a href={href({ view: "runs", q: "" })}>Runs</a> / {name}
          </div>
          <h1>
            {name} <KindBadge kind={kind} />
          </h1>
          <p className="muted">
            {t.provider} · <code>{t.model}</code> · {t.wire_format} wire format
          </p>
        </div>
        <div className="toolbar">
          {canCompare && (
            <a className="button" href={href({ view: "compare", a: compareTarget.a, b: compareTarget.b, step: null })}>
              Compare {kind === "candidate" ? "with baseline" : "with candidate"}
            </a>
          )}
          {canCompare && (
            <a className="button" href={href({ view: "drift", name })}>
              Drift report
            </a>
          )}
        </div>
      </header>

      <div className="stats">
        <Stat label="steps" value={t.steps.length} />
        <Stat label="model calls" value={t.steps.filter((s) => s.kind === "model").length} />
        <Stat label="tool calls" value={t.steps.filter((s) => s.kind === "tool").length} />
        <Stat label="tokens" value={formatTokens(t.usage.input_tokens + t.usage.output_tokens)} />
        <Stat label="latency" value={formatMs(t.total_latency_ms)} />
      </div>

      <div className="card">
        <div className="card-label">Tool sequence</div>
        <ToolChips
          names={t.steps.flatMap((s) => (s.kind === "tool" ? [s.name] : []))}
        />
      </div>
      {t.final_output && (
        <div className="card">
          <div className="card-label">Final output</div>
          <p className="final">{t.final_output}</p>
        </div>
      )}
      {t.notes.map((n) => (
        <p key={n} className="note">
          {n}
        </p>
      ))}

      <div className="timeline-head">
        <h2>Timeline</h2>
        <div className="toolbar">
          <button type="button" className="link" onClick={() => setExpanded(new Set(steps.map((_, i) => i)))}>
            Expand all
          </button>
          <button type="button" className="link" onClick={() => setExpanded(new Set())}>
            Collapse all
          </button>
        </div>
      </div>
      {steps.length === 0 ? (
        <p className="muted">This run has no steps.</p>
      ) : (
        <ol className="timeline">
          {steps.map((s, i) => (
            <li key={i}>
              <StepCard
                step={s}
                navIndex={i}
                active={i === active}
                expanded={expanded.has(i)}
                onToggle={() => toggle(i)}
                onFocus={() => setActive(i)}
              />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
