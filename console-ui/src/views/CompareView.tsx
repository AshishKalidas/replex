import { useMemo, useState } from "react";
import type { DataSource } from "../api";
import { DiffText, EmptyState, ErrorState, Loading, PageHead, Status, StepIcon } from "../components/common";
import { StepDetails } from "../components/StepCard";
import { inlineJson, stepSubtitle, stepTitle } from "../format";
import { useAsync, useKeyNav } from "../hooks";
import { href } from "../router";
import { readableDiff } from "../textdiff";
import type { CompareResult, DiffEntry, FieldChange, RunSummary, Step } from "../types";

const LEGEND: { status: DiffEntry["status"]; label: string; tone: "neutral" | "warn" | "ok" | "fail" }[] = [
  { status: "same", label: "unchanged", tone: "neutral" },
  { status: "changed", label: "changed", tone: "warn" },
  { status: "added", label: "added", tone: "ok" },
  { status: "removed", label: "removed", tone: "fail" },
];

function RunPicker({
  label,
  runs,
  value,
  onChange,
}: {
  label: string;
  runs: RunSummary[];
  value: string | null;
  onChange: (id: string) => void;
}) {
  return (
    <label className="picker">
      <span>{label}</span>
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="" disabled>
          Choose a run…
        </option>
        {runs.map((r) => (
          <option key={r.id} value={r.id}>
            {r.name} ({r.kind === "candidate" ? "latest run" : "recorded"})
          </option>
        ))}
      </select>
    </label>
  );
}

function Change({ change }: { change: FieldChange }) {
  if (typeof change.before === "string" && typeof change.after === "string" && change.path === "text") {
    return (
      <div className="change">
        <code className="change-path">{change.path}</code>
        <DiffText pieces={readableDiff(change.before, change.after)} />
      </div>
    );
  }
  return (
    <div className="change">
      <code className="change-path">{change.path}</code>
      <span className="change-before">{change.before === null ? "∅" : inlineJson(change.before, 60)}</span>
      <span className="change-arrow">→</span>
      <span className="change-after">{change.after === null ? "∅" : inlineJson(change.after, 60)}</span>
    </div>
  );
}

function Cell({ step }: { step: Step | undefined }) {
  if (!step) return <div className="cell cell-empty" />;
  return (
    <div className="cell">
      <div className="cell-title">
        <StepIcon kind={step.kind} />
        {step.kind === "tool" ? <code>{stepTitle(step)}</code> : <span>{stepTitle(step)}</span>}
      </div>
      <div className="cell-sub">{stepSubtitle(step)}</div>
    </div>
  );
}

export function DiffRows({ result, focusStep }: { result: CompareResult; focusStep: number | null }) {
  const entries = result.diff.entries;
  const initial = useMemo(() => {
    if (focusStep === null) return null;
    const i = entries.findIndex((e) => e.b_index === focusStep);
    return i >= 0 ? i : entries.findIndex((e) => e.a_index === focusStep);
  }, [entries, focusStep]);
  const [open, setOpen] = useState<Set<number>>(() => new Set(initial !== null && initial >= 0 ? [initial] : []));
  const toggle = (i: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  const [active, setActive] = useKeyNav(entries.length, toggle, initial !== null && initial >= 0 ? initial : null);

  if (entries.length === 0) return <EmptyState title="Both runs are empty." />;
  return (
    <div className="diff" role="list" aria-label="Aligned steps">
      <div className="diff-row diff-header" aria-hidden="true">
        <div className="mark" />
        <div>Before · {result.a.name}</div>
        <div>After · {result.b.name}</div>
      </div>
      {entries.map((e, i) => {
        const a = e.a_index === null ? undefined : result.a.steps[e.a_index];
        const b = e.b_index === null ? undefined : result.b.steps[e.b_index];
        return (
          <div
            key={i}
            role="listitem"
            data-nav-index={i}
            className={`diff-row diff-${e.status}${i === active ? " row-active" : ""}`}
          >
            <button
              type="button"
              className="diff-toggle"
              onClick={() => {
                setActive(i);
                toggle(i);
              }}
              aria-expanded={open.has(i)}
            >
              <span className="mark">
                <span className="visually-hidden">{e.status === "same" ? "unchanged" : e.status}: </span>
              </span>
              <Cell step={a} />
              <Cell step={b} />
            </button>
            {e.changes.length > 0 && (
              <div className="changes">
                {e.changes.slice(0, open.has(i) ? 50 : 3).map((c, k) => (
                  <Change key={k} change={c} />
                ))}
                {!open.has(i) && e.changes.length > 3 && (
                  <span className="secondary">and {e.changes.length - 3} more</span>
                )}
              </div>
            )}
            {open.has(i) && (
              <div className="diff-details">
                <div>{a ? <StepDetails step={a} /> : <p className="secondary">Not in the first run</p>}</div>
                <div>{b ? <StepDetails step={b} /> : <p className="secondary">Not in the second run</p>}</div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function CompareView({
  source,
  a,
  b,
  step,
}: {
  source: DataSource;
  a: string | null;
  b: string | null;
  step: number | null;
}) {
  const runs = useAsync(() => source.runs(), [source]);
  const ready = !!a && !!b && source.comparable(a, b);
  const result = useAsync<CompareResult | null>(
    () => (ready ? source.compare(a!, b!) : Promise.resolve(null)),
    [source, a, b, ready],
  );
  const go = (next: { a?: string; b?: string }) => {
    window.location.hash = href({ view: "compare", a: next.a ?? a, b: next.b ?? b, step: null });
  };
  const runList = runs.state === "ready" ? runs.data : [];
  const bChoices = source.mode === "static" && a ? runList.filter((r) => source.comparable(a, r.id)) : runList;

  return (
    <section>
      <PageHead
        title="Compare"
        lede="Two runs, aligned step by step. Select a row for the full details on both sides."
      />
      <div className="pickers">
        <RunPicker label="Before" runs={source.mode === "static" ? runList.filter((r) => r.kind === "baseline") : runList} value={a} onChange={(id) => go({ a: id })} />
        <span className="picker-arrow" aria-hidden="true">
          →
        </span>
        <RunPicker label="After" runs={bChoices} value={b} onChange={(id) => go({ b: id })} />
      </div>
      {!a || !b ? (
        <EmptyState title="Choose two runs.">Usually a recorded run and the latest run of the same scenario.</EmptyState>
      ) : !ready ? (
        <EmptyState title="These runs are not in this report.">
          A report holds each recorded run with its own latest run only.
        </EmptyState>
      ) : result.state === "loading" ? (
        <Loading what="comparison" />
      ) : result.state === "error" ? (
        <ErrorState error={result.error} />
      ) : result.data ? (
        <>
          <div className="legend">
            {LEGEND.map((l) => (
              <Status key={l.status} tone={l.tone}>
                {result.data!.diff.entries.filter((e) => e.status === l.status).length} {l.label}
              </Status>
            ))}
            {result.data.report && (
              <a href={href({ view: "scenario", name: result.data.report.name })} className="cta legend-report">
                {result.data.report.passed ? "Within policy" : "Breaks the policy"}
              </a>
            )}
          </div>
          <DiffRows key={`${a}|${b}`} result={result.data} focusStep={step} />
        </>
      ) : null}
    </section>
  );
}
