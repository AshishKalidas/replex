import { useState } from "react";
import { splitRunId, type DataSource } from "../api";
import { ErrorState, KindLabel, Loading, PageHead, Stat, ToolChips } from "../components/common";
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
  const compareTarget = kind === "candidate" ? { a: `baseline:${name}`, b: id } : { a: id, b: `candidate:${name}` };
  const canCompare = source.comparable(compareTarget.a, compareTarget.b);
  const allOpen = steps.length > 0 && expanded.size === steps.length;

  return (
    <section>
      <PageHead
        eyebrow={
          <>
            <a href={href({ view: "runs", q: "" })}>Runs</a>
            <span className="crumb-sep" aria-hidden="true">
              ›
            </span>
            <KindLabel kind={kind} />
          </>
        }
        title={name}
        lede={
          <>
            {t.model} via {t.provider === "mock" ? "the mock provider" : t.provider}, {t.wire_format} wire format.
          </>
        }
        actions={
          canCompare && (
            <>
              <a className="button" href={href({ view: "compare", a: compareTarget.a, b: compareTarget.b, step: null })}>
                Compare with the {kind === "candidate" ? "recorded run" : "latest run"}
              </a>
              <a className="cta" href={href({ view: "scenario", name })}>
                Review this scenario
              </a>
            </>
          )
        }
      />

      <div className="stats">
        <Stat label="Steps" value={t.steps.length} />
        <Stat label="Model calls" value={t.steps.filter((s) => s.kind === "model").length} />
        <Stat label="Tool calls" value={t.steps.filter((s) => s.kind === "tool").length} />
        <Stat label="Tokens" value={formatTokens(t.usage.input_tokens + t.usage.output_tokens)} />
        <Stat label="Latency" value={formatMs(t.total_latency_ms)} />
      </div>

      <div className="panel">
        <div className="panel-row">
          <div className="panel-label">Tools, in order</div>
          <ToolChips names={t.steps.flatMap((s) => (s.kind === "tool" ? [s.name] : []))} />
        </div>
        {t.final_output && (
          <div className="panel-row">
            <div className="panel-label">Final answer</div>
            <p className="final">{t.final_output}</p>
          </div>
        )}
      </div>
      {t.notes.map((n) => (
        <p key={n} className="note">
          {n}
        </p>
      ))}

      <div className="section-head">
        <h2>Timeline</h2>
        {steps.length > 0 && (
          <button
            type="button"
            className="link"
            onClick={() => setExpanded(allOpen ? new Set() : new Set(steps.map((_, i) => i)))}
          >
            {allOpen ? "Collapse all" : "Expand all"}
          </button>
        )}
      </div>
      {steps.length === 0 ? (
        <p className="secondary">This run has no steps.</p>
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
