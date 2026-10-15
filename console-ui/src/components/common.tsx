import { useState, type ReactNode } from "react";
import { toJson } from "../format";
import type { Piece } from "../textdiff";

export function Logo() {
  return (
    <svg className="logo" viewBox="0 0 24 24" aria-hidden="true">
      <rect width="24" height="24" rx="6.5" />
      <path d="M8 17V7h4.6a3.2 3.2 0 0 1 0 6.4H8m4.4 0L16 17" />
    </svg>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <p>Loading {what}…</p>
    </div>
  );
}

export function ErrorState({ error }: { error: string }) {
  return (
    <div className="state" role="alert">
      <h2>Something went wrong.</h2>
      <pre className="state-error">{error}</pre>
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state">
      <h2>{title}</h2>
      {children && <div className="state-body">{children}</div>}
      {action && <div className="state-action">{action}</div>}
    </div>
  );
}

/** Page header: small eyebrow, large headline, one-sentence lede, optional actions. */
export function PageHead({
  eyebrow,
  title,
  lede,
  actions,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  lede?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-head">
      {eyebrow && <div className="eyebrow">{eyebrow}</div>}
      <h1>{title}</h1>
      {lede && <p className="lede">{lede}</p>}
      {actions && <div className="actions">{actions}</div>}
    </header>
  );
}

export type Tone = "ok" | "fail" | "warn" | "info" | "neutral";

/** A status in words with a small colored dot: color is never the only signal. */
export function Status({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`status status-${tone}`} title={title}>
      <span className="dot" aria-hidden="true" />
      {children}
    </span>
  );
}

/** The status of a run in the same words the overview uses. */
export function DriftStatus({ drift, warnings = 0 }: { drift: "passed" | "failed" | null | undefined; warnings?: number }) {
  if (!drift) return <Status tone="neutral">Not re-run yet</Status>;
  if (drift === "failed") return <Status tone="fail">Behaviour changed</Status>;
  return warnings ? <Status tone="warn">Small differences</Status> : <Status tone="ok">Behaves as recorded</Status>;
}

export function KindLabel({ kind }: { kind: string }) {
  return <span className="kind">{kind === "candidate" ? "Latest run" : "Recorded"}</span>;
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: Tone }) {
  return (
    <div className={`stat${tone ? ` stat-${tone}` : ""}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export function ToolChips({ names, highlight }: { names: string[]; highlight?: Set<string> }) {
  if (!names.length) return <span className="quiet">No tool calls</span>;
  return (
    <span className="chips">
      {names.map((n, i) => (
        <span key={`${n}-${i}`} className="chip-wrap">
          {i > 0 && (
            <span className="chip-arrow" aria-hidden="true">
              ›
            </span>
          )}
          <span className={`chip${highlight?.has(n) ? " chip-new" : ""}`}>{n}</span>
        </span>
      ))}
    </span>
  );
}

export function Segmented({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { id: string; label: string }[];
  onChange: (id: string) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          role="radio"
          aria-checked={value === o.id}
          className={value === o.id ? "on" : ""}
          onClick={() => onChange(o.id)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** JSON (or text) block that collapses when long. */
export function JsonBlock({ value, label, lines = 10 }: { value: unknown; label?: string; lines?: number }) {
  const text = typeof value === "string" ? value : toJson(value);
  const long = text.split("\n").length > lines || text.length > 900;
  const [open, setOpen] = useState(!long);
  return (
    <div className="block">
      {label && <div className="block-label">{label}</div>}
      <pre className={`code${open ? "" : " code-collapsed"}`}>{text}</pre>
      {long && (
        <button type="button" className="link" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Show less" : `Show all ${text.split("\n").length} lines`}
        </button>
      )}
    </div>
  );
}

export function Collapsible({ title, children, defaultOpen = false }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <details className="collapsible" open={defaultOpen}>
      <summary>{title}</summary>
      <div className="collapsible-body">{children}</div>
    </details>
  );
}

export function DiffText({ pieces }: { pieces: Piece[] }) {
  return (
    <p className="difftext">
      {pieces.map((p, i) =>
        p.op === "same" ? (
          <span key={i}>{p.text}</span>
        ) : p.op === "add" ? (
          <ins key={i}>{p.text}</ins>
        ) : (
          <del key={i}>{p.text}</del>
        ),
      )}
    </p>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd>{children}</kbd>;
}

export function StepIcon({ kind }: { kind: "model" | "tool" }) {
  return (
    <span className={`step-icon step-icon-${kind}`} aria-hidden="true">
      {kind === "model" ? (
        <svg viewBox="0 0 16 16">
          <path d="M3 4.5A1.5 1.5 0 0 1 4.5 3h7A1.5 1.5 0 0 1 13 4.5v5a1.5 1.5 0 0 1-1.5 1.5H7l-3 2.5V11h.5" />
        </svg>
      ) : (
        <svg viewBox="0 0 16 16">
          <path d="M10.2 2.6a3 3 0 0 0-3.7 3.9L2.8 10.2a1.3 1.3 0 0 0 1.9 1.9l3.7-3.7a3 3 0 0 0 3.9-3.7L10.5 6.5 9 6l-.5-1.5z" />
        </svg>
      )}
    </span>
  );
}
