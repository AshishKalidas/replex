// Plain-language summaries of drift reports, for people who did not write the policy.
import type { DriftReport, Finding, RunSummary } from "./types";

/** "refund_ineligible_openai" -> "Refund ineligible openai". The raw name is shown alongside. */
export function humanize(name: string): string {
  const words = name.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return words ? words[0]!.toUpperCase() + words.slice(1) : name;
}

/**
 * Where a scenario stands:
 * - changed: the latest run breaks the policy and needs a decision
 * - minor: it differs in ways the policy only warns about
 * - same: it behaves as recorded
 * - pending: recorded, but not re-run since
 */
export type Verdict = "changed" | "minor" | "same" | "pending";

export function verdictOf(report: DriftReport | null): Verdict {
  if (!report) return "pending";
  if (!report.passed) return "changed";
  return report.findings.some((f) => f.severity === "warn") ? "minor" : "same";
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  changed: "Behaviour changed",
  minor: "Small differences",
  same: "Behaves as recorded",
  pending: "Not re-run yet",
};

export const VERDICT_TONE: Record<Verdict, "fail" | "warn" | "ok" | "neutral"> = {
  changed: "fail",
  minor: "warn",
  same: "ok",
  pending: "neutral",
};

function list(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function sentence(f: Finding, sequenceChanged: boolean): string | null {
  switch (f.check) {
    case "tool_sequence": {
      const before = strings(f.before);
      const after = strings(f.after);
      const dropped = [...new Set(before.filter((n) => !after.includes(n)))];
      const added = [...new Set(after.filter((n) => !before.includes(n)))];
      const parts: string[] = [];
      if (dropped.length) parts.push(`stopped calling ${list(dropped)}`);
      if (added.length) parts.push(`started calling ${list(added)}`);
      if (!parts.length) return "Calls the same tools in a different order.";
      const text = parts.join(" and ");
      return `${text[0]!.toUpperCase()}${text.slice(1)}.`;
    }
    case "new_tools":
      // tool_sequence already names the new tools when both checks fire.
      return sequenceChanged ? null : `Started calling ${list(strings(f.after))}.`;
    case "forbidden_tools":
      return `Called ${String(f.after)}, which the policy forbids.`;
    case "tool_args": {
      const m = /'([^']+)'/.exec(f.message);
      return `Calls ${m ? m[1] : "a tool"} with different arguments.`;
    }
    case "final_output": {
      if (f.message.includes("not valid JSON") || f.message.includes("violates the schema")) {
        return "The final answer no longer has the required format.";
      }
      const score = /similarity ([\d.]+)/.exec(f.message);
      if (score) return `The final answer is worded differently (${Math.round(Number(score[1]) * 100)}% similar).`;
      return "The final answer changed.";
    }
    case "steps":
      return `Takes ${String(f.after)} steps where it used to take ${String(f.before)}.`;
    case "tokens": {
      const before = Number(f.before);
      const after = Number(f.after);
      const pct = before ? Math.round(((after - before) / before) * 100) : 0;
      return `Uses ${pct}% more tokens.`;
    }
    default:
      return null;
  }
}

/** One sentence per meaningful difference, most important first. Latency is left out. */
export function explain(report: DriftReport): string[] {
  const out: string[] = [];
  const order = ["fail", "warn"];
  const findings = [...report.findings]
    .filter((f) => f.severity !== "info")
    .sort((a, b) => order.indexOf(a.severity) - order.indexOf(b.severity));
  const sequenceChanged = findings.some((f) => f.check === "tool_sequence");
  for (const f of findings) {
    const text = sentence(f, sequenceChanged);
    if (text && !out.includes(text)) out.push(text);
  }
  return out;
}

/** A one-line verdict for a scenario, written for someone skimming. */
export function headline(report: DriftReport | null): string {
  const v = verdictOf(report);
  if (!report || v === "pending") return "Recorded, and not re-run since. Run a drift check to compare it.";
  const lines = explain(report);
  if (v === "same") return "The latest run does the same thing as the recorded one.";
  return lines[0] ?? VERDICT_LABEL[v];
}

export interface Scenario {
  name: string;
  baseline: RunSummary | null;
  candidate: RunSummary | null;
}

/** Group runs into scenarios: one baseline and, after a drift check, its latest candidate. */
export function scenarios(runs: RunSummary[]): Scenario[] {
  const byName = new Map<string, Scenario>();
  for (const r of runs) {
    const s = byName.get(r.name) ?? { name: r.name, baseline: null, candidate: null };
    if (r.kind === "candidate") s.candidate = r;
    else s.baseline = r;
    byName.set(r.name, s);
  }
  const rank = (s: Scenario) => (s.candidate?.drift === "failed" ? 0 : s.candidate ? (s.candidate.warnings ? 1 : 2) : 3);
  return [...byName.values()].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}
