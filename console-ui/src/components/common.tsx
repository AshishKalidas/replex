import { useState, type ReactNode } from "react";
import { toJson } from "../format";
import type { Piece } from "../textdiff";

export function Loading({ what }: { what: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" /> Loading {what}…
    </div>
  );
}

export function ErrorState({ error }: { error: string }) {
  return (
    <div className="state state-error" role="alert">
      <strong>Could not load this view.</strong>
      <pre>{error}</pre>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <strong>{title}</strong>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}

export function Pill({ tone, children, title }: { tone: string; children: ReactNode; title?: string }) {
  return (
    <span className={`pill pill-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function DriftPill({ drift, warnings = 0 }: { drift: "passed" | "failed" | null | undefined; warnings?: number }) {
  if (!drift) return <span className="muted">—</span>;
  return drift === "passed" ? (
    <Pill tone="ok" title={warnings ? `${warnings} warning(s)` : undefined}>
      ✓ passed{warnings ? ` · ${warnings}⚠` : ""}
    </Pill>
  ) : (
    <Pill tone="fail">✗ drift</Pill>
  );
}

export function KindBadge({ kind }: { kind: string }) {
  return <span className={`kind kind-${kind}`}>{kind}</span>;
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export function ToolChips({ names, highlight }: { names: string[]; highlight?: Set<string> }) {
  if (!names.length) return <span className="muted">no tool calls</span>;
  return (
    <span className="chips">
      {names.map((n, i) => (
        <span key={`${n}-${i}`} className={`chip${highlight?.has(n) ? " chip-hot" : ""}`}>
          {i > 0 && <span className="chip-arrow" aria-hidden="true">→</span>}
          {n}
        </span>
      ))}
    </span>
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
          {open ? "Collapse" : `Show all (${text.split("\n").length} lines)`}
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
