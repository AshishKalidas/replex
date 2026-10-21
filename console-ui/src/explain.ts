// Plain-language summaries of drift reports, for people who did not write the policy.
import { costChangePct } from "./cost";
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

/** Tool names as people read them: “Check refund policy”. */
function quoted(names: string[]): string {
  return list(names.map((n) => `“${humanize(n)}”`));
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** Free text as one sentence: capitalised and ending in punctuation. */
function asSentence(text: string): string {
  const t = text.trim();
  if (!t) return t;
  const capitalised = t[0]!.toUpperCase() + t.slice(1);
  return /[.!?]$/.test(capitalised) ? capitalised : `${capitalised}.`;
}

function sentence(f: Finding, sequenceChanged: boolean): string | null {
  switch (f.check) {
    case "tool_sequence": {
      const before = strings(f.before);
      const after = strings(f.after);
      const dropped = [...new Set(before.filter((n) => !after.includes(n)))];
      const added = [...new Set(after.filter((n) => !before.includes(n)))];
      if (dropped.length && added.length) return `Skips ${quoted(dropped)} and uses ${quoted(added)} instead.`;
      if (dropped.length) return `Skips ${quoted(dropped)}.`;
      if (added.length) return `Now also uses ${quoted(added)}.`;
      return "Uses the same tools in a different order.";
    }
    case "new_tools":
      // tool_sequence already names the new tools when both checks fire.
      return sequenceChanged ? null : `Now also uses ${quoted(strings(f.after))}.`;
    case "forbidden_tools":
      return `Used ${quoted([String(f.after)])}, which your policy forbids.`;
    case "tool_args": {
      const m = /'([^']+)'/.exec(f.message);
      return `Uses ${m ? quoted([m[1]!]) : "a tool"} with different inputs.`;
    }
    case "final_output": {
      if (f.reason) return `The final answer means something different: ${asSentence(f.reason)}`;
      if (f.message.startsWith("final output judge error")) {
        return "The final answer could not be checked: the judge model gave no verdict.";
      }
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
    case "cost": {
      const pct = costChangePct(f.before, f.after);
      return pct === null ? "Costs more per run." : `Costs ${pct}% more per run.`;
    }
    default:
      return null;
  }
}

/** One sentence per meaningful difference, most important first. Reported-only findings are left out. */
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
  if (!report || v === "pending") return "Recorded. Not checked against a change yet.";
  const lines = explain(report);
  if (v === "same") return "Still handled exactly as recorded.";
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
