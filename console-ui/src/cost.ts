// What a run costs, in words people skim. Costs are US dollars; null means no known price.
import type { DriftReport } from "./types";

export function formatCost(amount: number | null | undefined): string {
  if (amount === undefined) return "—";
  if (amount === null) return "Unknown";
  if (amount === 0) return "$0.00";
  if (amount < 0.0001) return "<$0.0001";
  if (amount < 1) return `$${amount.toFixed(4)}`;
  return `$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Percentage change from `before` to `after`, or null when it cannot be computed. */
export function costChangePct(before: unknown, after: unknown): number | null {
  if (typeof before !== "number" || typeof after !== "number" || before === 0) return null;
  return Math.round(((after - before) / before) * 100);
}

/** "Latest run costs 94% less." when both runs have a known cost that differs. */
export function costChange(report: DriftReport): string | null {
  const pct = costChangePct(report.baseline.cost, report.candidate.cost);
  if (pct === null || pct === 0) return null;
  return `Latest run costs ${Math.abs(pct)}% ${pct > 0 ? "more" : "less"}.`;
}
