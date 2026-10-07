import { useMemo, useState } from "react";
import type { DataSource } from "../api";
import { DiffText, EmptyState, ErrorState, Loading, Pill } from "../components/common";
import { StepDetails } from "../components/StepCard";
import { inlineJson, stepSubtitle, stepTitle } from "../format";
import { useAsync, useKeyNav } from "../hooks";
import { href } from "../router";
import { wordDiff } from "../textdiff";
import type { CompareResult, DiffEntry, FieldChange, RunSummary, Step } from "../types";

const MARK: Record<DiffEntry["status"], string> = { same: "=", changed: "~", added: "+", removed: "−" };

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
            {r.kind}: {r.name}
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
        <DiffText pieces={wordDiff(change.before, change.after)} />
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
        <span className="step-index">{step.index}</span>
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

  if (entries.length === 0) return <EmptyState title="Both runs are empty" />;
  return (
    <div className="diff" role="table" aria-label="Aligned steps">
      <div className="diff-row diff-header" role="row">
        <div className="mark" />
        <div role="columnheader">{result.a.name} (A)</div>
        <div role="columnheader">{result.b.name} (B)</div>
      </div>
      {entries.map((e, i) => {
        const a = e.a_index === null ? undefined : result.a.steps[e.a_index];
        const b = e.b_index === null ? undefined : result.b.steps[e.b_index];
        return (
          <div
            key={i}
            role="row"
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
              aria-label={`${e.status} ${e.name}`}
            >
              <span className="mark">{MARK[e.status]}</span>
              <Cell step={a} />
              <Cell step={b} />
            </button>
            {e.changes.length > 0 && (
              <div className="changes">
                {e.changes.slice(0, open.has(i) ? 50 : 3).map((c, k) => (
                  <Change key={k} change={c} />
                ))}
                {!open.has(i) && e.changes.length > 3 && (
                  <span className="muted small">+{e.changes.length - 3} more</span>
                )}
              </div>
            )}
            {open.has(i) && (
              <div className="diff-details">
                <div>{a ? <StepDetails step={a} /> : <p className="muted">Not in A</p>}</div>
                <div>{b ? <StepDetails step={b} /> : <p className="muted">Not in B</p>}</div>
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
      <header className="page-head">
        <div>
          <h1>Compare</h1>
          <p className="muted">Steps aligned by type and tool name. Click a row or press Enter for details.</p>
        </div>
      </header>
      <div className="pickers">
        <RunPicker label="A · before" runs={source.mode === "static" ? runList.filter((r) => r.kind === "baseline") : runList} value={a} onChange={(id) => go({ a: id })} />
        <span className="picker-arrow" aria-hidden="true">⇄</span>
        <RunPicker label="B · after" runs={bChoices} value={b} onChange={(id) => go({ b: id })} />
      </div>
      {!a || !b ? (
        <EmptyState title="Pick two runs to compare">
          Typically a baseline (A) and the candidate from a drift run (B).
        </EmptyState>
      ) : !ready ? (
        <EmptyState title="These runs are not in this report">
          A static report holds each baseline with its own candidate only.
        </EmptyState>
      ) : result.state === "loading" ? (
        <Loading what="comparison" />
      ) : result.state === "error" ? (
        <ErrorState error={result.error} />
      ) : result.data ? (
        <>
          <div className="legend">
            {(["same", "changed", "added", "removed"] as const).map((s) => (
              <span key={s} className={`legend-item diff-${s}`}>
                <span className="mark">{MARK[s]}</span> {result.data!.diff.entries.filter((e) => e.status === s).length} {s}
              </span>
            ))}
            {result.data.report && (
              <a href={href({ view: "drift", name: result.data.report.name })} className="legend-report">
                {result.data.report.passed ? <Pill tone="ok">policy passed</Pill> : <Pill tone="fail">policy failed</Pill>}
              </a>
            )}
          </div>
          <DiffRows key={`${a}|${b}`} result={result.data} focusStep={step} />
        </>
      ) : null}
    </section>
  );
}
